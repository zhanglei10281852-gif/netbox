"""
Tests for Idempotency-Key support on REST API write requests.

An unsafe request (POST/PUT/PATCH/DELETE) carrying an Idempotency-Key header records its
outcome; identical retries replay the stored response without re-executing the write, while
same-key requests with different semantics are rejected with HTTP 409. See netbox/api/idempotency.py.
"""
import threading
import traceback
from datetime import timedelta
from unittest.mock import patch

from django.contrib.contenttypes.models import ContentType
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from core.choices import ObjectChangeActionChoices
from core.jobs import SystemHousekeepingJob
from core.models import IdempotencyKey, Job, ObjectChange, ObjectType
from dcim.models import Region
from netbox.api.idempotency import IdempotencyExchange
from users.constants import TOKEN_PREFIX
from users.models import ObjectPermission, Token, User
from utilities.testing.api import APITestCase
from utilities.testing.mixins import RQQueueTestMixin

URL = '/api/dcim/regions/'


class IdempotencyKeyTests(RQQueueTestMixin, APITestCase):

    @classmethod
    def setUpTestData(cls):
        cls.regions = [
            Region.objects.create(name=f'Region {i}', slug=f'region-{i}')
            for i in range(1, 4)
        ]

    def setUp(self):
        super().setUp()

        # Run enqueued background jobs inline (immediate=True) so they execute within the test.
        import netbox.jobs as jobs_module
        self._orig_enqueue = jobs_module.AsyncAPIJob.enqueue.__func__

        def _immediate_enqueue(cls_, *args, **kwargs):
            kwargs.setdefault('immediate', True)
            return self._orig_enqueue(cls_, *args, **kwargs)

        jobs_module.AsyncAPIJob.enqueue = classmethod(_immediate_enqueue)
        self.addCleanup(
            setattr, jobs_module.AsyncAPIJob, 'enqueue', classmethod(self._orig_enqueue)
        )

        # Report a worker as available so _enqueue_bulk_job's liveness guard passes.
        worker_patcher = patch(
            'netbox.api.viewsets.mixins.any_workers_for_queue', return_value=True
        )
        worker_patcher.start()
        self.addCleanup(worker_patcher.stop)

    def grant(self, *actions):
        perm = ObjectPermission.objects.create(name='Test permission', actions=list(actions))
        perm.users.add(self.user)
        perm.object_types.add(ContentType.objects.get_for_model(Region))

    def _key_header(self, key):
        return {**self.header, 'HTTP_IDEMPOTENCY_KEY': key}

    def _object_changes(self, region, action):
        return ObjectChange.objects.filter(
            changed_object_type=ObjectType.objects.get_for_model(Region),
            changed_object_id=region.pk,
            action=action,
        )

    # ------------------------------------------------------------ basics

    def test_first_request_records_and_retry_replays(self):
        self.grant('add', 'view')
        payload = {'name': 'Idem Region', 'slug': 'idem-region'}

        r1 = self.client.post(URL, payload, format='json', **self._key_header('key-1'))
        self.assertHttpStatus(r1, status.HTTP_201_CREATED)
        self.assertIsNone(r1.get('Idempotency-Replayed'))

        region = Region.objects.get(slug='idem-region')
        record = IdempotencyKey.objects.get(scope_hash__isnull=False)
        self.assertEqual(record.method, 'POST')
        self.assertEqual(record.path, URL.rstrip('/'))
        self.assertEqual(record.status, IdempotencyKey.STATUS_COMPLETE)
        self.assertEqual(record.response_status, 201)
        self.assertEqual(record.response_headers.get('Location'), r1['Location'])
        self.assertIn(str(region.pk), record.response_body)

        # Replay: same key, same body
        r2 = self.client.post(URL, payload, format='json', **self._key_header('key-1'))
        self.assertHttpStatus(r2, status.HTTP_201_CREATED)
        self.assertEqual(r2['Idempotency-Replayed'], 'true')
        self.assertEqual(r2.json(), r1.json())

        # Exactly one object and one changelog entry were produced
        self.assertEqual(Region.objects.filter(slug='idem-region').count(), 1)
        self.assertEqual(self._object_changes(region, ObjectChangeActionChoices.ACTION_CREATE).count(), 1)

    def test_replay_does_not_queue_events(self):
        self.grant('add', 'view')
        payload = {'name': 'Event Region', 'slug': 'event-region'}
        with patch('netbox.context_managers.flush_events') as flush:
            self.client.post(URL, payload, format='json', **self._key_header('ev'))
            self.client.post(URL, payload, format='json', **self._key_header('ev'))
        # The original request queues events; the replay runs no handler and queues nothing.
        self.assertEqual(flush.call_count, 1)

    def test_put_patch_delete_replay(self):
        self.grant('add', 'change', 'delete', 'view')
        region = Region.objects.create(name='To Change', slug='to-change')
        detail = f'{URL}{region.pk}/'

        # PATCH replay
        patch_payload = {'name': 'Changed Name'}
        p1 = self.client.patch(detail, patch_payload, format='json', **self._key_header('k-patch'))
        self.assertHttpStatus(p1, status.HTTP_200_OK)
        region.refresh_from_db()
        self.assertEqual(region.name, 'Changed Name')

        p2 = self.client.patch(detail, patch_payload, format='json', **self._key_header('k-patch'))
        self.assertHttpStatus(p2, status.HTTP_200_OK)
        self.assertEqual(p2['Idempotency-Replayed'], 'true')
        self.assertEqual(p2.json(), p1.json())
        self.assertEqual(self._object_changes(region, ObjectChangeActionChoices.ACTION_UPDATE).count(), 1)

        # DELETE replay: the retried delete replays 204 instead of 404ing on the missing object
        d1 = self.client.delete(detail, **self._key_header('k-delete'))
        self.assertHttpStatus(d1, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Region.objects.filter(pk=region.pk).exists())

        d2 = self.client.delete(detail, **self._key_header('k-delete'))
        self.assertHttpStatus(d2, status.HTTP_204_NO_CONTENT)
        self.assertEqual(d2['Idempotency-Replayed'], 'true')
        self.assertEqual(
            self._object_changes(region, ObjectChangeActionChoices.ACTION_DELETE).count(), 1
        )

        # A non-idempotent delete of the missing object does behave as before (404)
        d3 = self.client.delete(detail, **self.header)
        self.assertHttpStatus(d3, status.HTTP_404_NOT_FOUND)

    # ------------------------------------------------------------ conflicts

    def test_same_key_different_body_conflicts(self):
        self.grant('add', 'view')
        self.client.post(
            URL, {'name': 'First', 'slug': 'first'}, format='json', **self._key_header('shared')
        )
        with patch('netbox.context_managers.flush_events'):
            r = self.client.post(
                URL, {'name': 'Second', 'slug': 'second'}, format='json', **self._key_header('shared')
            )
        self.assertHttpStatus(r, status.HTTP_409_CONFLICT)
        self.assertIsNone(r.get('Idempotency-Replayed'))
        self.assertFalse(Region.objects.filter(slug='second').exists())
        self.assertEqual(Region.objects.filter(slug='first').count(), 1)

    def test_response_shaping_query_params_do_not_conflict(self):
        self.grant('add', 'view')
        payload = {'name': 'Fields Region', 'slug': 'fields-region'}
        # Restricting the response fields also limits serializer input to those fields, so include
        # the required ones on the first request; the point here is that response-shaping params
        # (fields/omit) are excluded from the idempotency fingerprint.
        r1 = self.client.post(
            f'{URL}?fields=name,slug', payload, format='json', **self._key_header('fields')
        )
        self.assertHttpStatus(r1, status.HTTP_201_CREATED)
        self.assertIn('name', r1.json())
        self.assertNotIn('url', r1.json())

        # A different response-shaping parameter (?fields vs ?omit) keeps the same semantics
        r2 = self.client.post(
            f'{URL}?omit=url', payload, format='json', **self._key_header('fields')
        )
        self.assertHttpStatus(r2, status.HTTP_201_CREATED)
        self.assertEqual(r2['Idempotency-Replayed'], 'true')
        # The replayed body is exactly the first (fields-limited) response
        self.assertEqual(r2.json(), r1.json())

    def test_background_param_is_significant_and_replays_202(self):
        self.grant('add', 'view')
        payload = [
            {'name': 'Bulk A', 'slug': 'bulk-a'},
            {'name': 'Bulk B', 'slug': 'bulk-b'},
        ]

        def bulk_jobs():
            return Job.objects.filter(name='Bulk create regions')

        r1 = self.client.post(
            f'{URL}?background=true', payload, format='json', **self._key_header('bg')
        )
        self.assertHttpStatus(r1, status.HTTP_202_ACCEPTED)
        self.assertIsNone(r1.get('Idempotency-Replayed'))
        job_id = r1.json()['job']['id']
        self.assertEqual(bulk_jobs().count(), 1)
        self.assertEqual(Region.objects.filter(slug__in=('bulk-a', 'bulk-b')).count(), 2)

        # Replay: same 202/job reference, no second enqueue
        r2 = self.client.post(
            f'{URL}?background=true', payload, format='json', **self._key_header('bg')
        )
        self.assertHttpStatus(r2, status.HTTP_202_ACCEPTED)
        self.assertEqual(r2['Idempotency-Replayed'], 'true')
        self.assertEqual(r2.json()['job']['id'], job_id)
        self.assertEqual(bulk_jobs().count(), 1)

        # Same key with a different write-significant parameter (background vs synchronous) → 409
        r3 = self.client.post(URL, payload, format='json', **self._key_header('bg'))
        self.assertHttpStatus(r3, status.HTTP_409_CONFLICT)

    # ------------------------------------------------------------ passthrough / auth

    def test_without_key_passes_through(self):
        self.grant('add', 'view')
        r1 = self.client.post(URL, {'name': 'No Key 1', 'slug': 'no-key-1'}, format='json', **self.header)
        r2 = self.client.post(URL, {'name': 'No Key 2', 'slug': 'no-key-2'}, format='json', **self.header)
        self.assertHttpStatus(r1, status.HTTP_201_CREATED)
        self.assertHttpStatus(r2, status.HTTP_201_CREATED)
        self.assertEqual(Region.objects.filter(slug__in=('no-key-1', 'no-key-2')).count(), 2)
        self.assertEqual(IdempotencyKey.objects.count(), 0)

    def test_read_only_requests_ignore_key(self):
        self.grant('view')
        r = self.client.get(URL, **self._key_header('ignored'))
        self.assertHttpStatus(r, status.HTTP_200_OK)
        self.assertEqual(IdempotencyKey.objects.count(), 0)

    def test_authentication_failure_not_recorded(self):
        self.grant('add', 'view')
        bad_auth = {'HTTP_AUTHORIZATION': 'Bearer not-a-valid-token', 'HTTP_IDEMPOTENCY_KEY': 'auth'}
        r = self.client.post(URL, {'name': 'X', 'slug': 'x'}, format='json', **bad_auth)
        # NetBox rejects unauthenticated write requests with 401/403 before the idempotency check
        self.assertIn(r.status_code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))
        self.assertEqual(IdempotencyKey.objects.count(), 0)
        # The same key can then be used by an authenticated request
        r2 = self.client.post(
            URL, {'name': 'After Auth', 'slug': 'after-auth'}, format='json', **self._key_header('auth')
        )
        self.assertHttpStatus(r2, status.HTTP_201_CREATED)

    def test_malformed_key_rejected(self):
        self.grant('add', 'view')
        kwargs = {'format': 'json', **self.header}
        for bad_key in ('', 'x' * 256, 'bad\nkey'):
            r = self.client.post(
                URL, {'name': 'Bad', 'slug': f'bad-{len(bad_key)}'},
                **kwargs, HTTP_IDEMPOTENCY_KEY=bad_key,
            )
            self.assertHttpStatus(r, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(IdempotencyKey.objects.count(), 0)

    def test_plain_http_response_handled(self):
        """A keyed endpoint returning a plain Django HttpResponse (e.g. a plugin @action)
        is recorded and replayed without requiring DRF's .render()."""
        from django.http import HttpResponse as DjangoHttpResponse

        from dcim.api.views import RegionViewSet

        self.grant('add', 'view')
        original = RegionViewSet.create
        try:
            def fake_create(view_self, request, *args, **kwargs):
                return DjangoHttpResponse('plain-body', content_type='text/plain', status=201)

            RegionViewSet.create = fake_create
            kwargs = {'format': 'json', **self._key_header('plain')}
            r1 = self.client.post(URL, {'name': 'X', 'slug': 'x'}, **kwargs)
            self.assertEqual(r1.status_code, status.HTTP_201_CREATED)
            r2 = self.client.post(URL, {'name': 'X', 'slug': 'x'}, **kwargs)
        finally:
            RegionViewSet.create = original
        self.assertEqual(r2.status_code, status.HTTP_201_CREATED)
        self.assertEqual(r2['Idempotency-Replayed'], 'true')
        self.assertEqual(r2.content, b'plain-body')

    def test_validation_error_is_replayed(self):
        self.grant('add', 'view')
        bad_payload = {'name': 'Missing Slug'}
        r1 = self.client.post(URL, bad_payload, format='json', **self._key_header('bad-body'))
        self.assertHttpStatus(r1, status.HTTP_400_BAD_REQUEST)
        r2 = self.client.post(URL, bad_payload, format='json', **self._key_header('bad-body'))
        self.assertHttpStatus(r2, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(r2['Idempotency-Replayed'], 'true')
        self.assertEqual(r2.json(), r1.json())
        self.assertEqual(Region.objects.filter(name='Missing Slug').count(), 0)

    def test_server_error_releases_placeholder(self):
        self.grant('add', 'view')
        payload = [{'name': 'Retry Me', 'slug': 'retry-me'}]

        # No RQ worker: the background request fails (503) before anything is enqueued
        with patch('netbox.api.viewsets.mixins.any_workers_for_queue', return_value=False):
            r1 = self.client.post(
                f'{URL}?background=true', payload, format='json', **self._key_header('retry-5xx')
            )
        self.assertHttpStatus(r1, status.HTTP_503_SERVICE_UNAVAILABLE)
        # The placeholder was rolled back: nothing blocks a retry
        self.assertEqual(IdempotencyKey.objects.count(), 0)

        # Same key, worker now available (setUp patch): executes fresh, succeeds
        r2 = self.client.post(
            f'{URL}?background=true', payload, format='json', **self._key_header('retry-5xx')
        )
        self.assertHttpStatus(r2, status.HTTP_202_ACCEPTED)
        self.assertIsNone(r2.get('Idempotency-Replayed'))
        self.assertEqual(Region.objects.filter(slug='retry-me').count(), 1)

    # ------------------------------------------------------------ isolation / config / retention

    def test_scope_isolated_by_user(self):
        self.grant('add', 'view')
        other = User.objects.create_user(username='otheruser')
        other_token = Token.objects.create(user=other)
        perm = ObjectPermission.objects.get(name='Test permission')
        perm.users.add(other)
        other_header = {
            'HTTP_AUTHORIZATION': f'Bearer {TOKEN_PREFIX}{other_token.key}.{other_token.token}',
        }

        payload_a = {'name': 'User A', 'slug': 'user-a'}
        payload_b = {'name': 'User B', 'slug': 'user-b'}
        r1 = self.client.post(
            URL, payload_a, format='json', **self.header, HTTP_IDEMPOTENCY_KEY='shared-key'
        )
        r2 = self.client.post(
            URL, payload_b, format='json', **other_header, HTTP_IDEMPOTENCY_KEY='shared-key'
        )
        self.assertHttpStatus(r1, status.HTTP_201_CREATED)
        self.assertHttpStatus(r2, status.HTTP_201_CREATED)
        self.assertIsNone(r1.get('Idempotency-Replayed'))
        self.assertIsNone(r2.get('Idempotency-Replayed'))
        self.assertTrue(Region.objects.filter(slug='user-a').exists())
        self.assertTrue(Region.objects.filter(slug='user-b').exists())
        self.assertEqual(IdempotencyKey.objects.filter(idempotency_key='shared-key').count(), 2)

    @override_settings(IDEMPOTENCY_KEY_ENABLED=False)
    def test_feature_disabled(self):
        self.grant('add', 'view')
        r = self.client.post(
            URL, {'name': 'Disabled', 'slug': 'disabled'}, format='json',
            **self.header, HTTP_IDEMPOTENCY_KEY='x',
        )
        self.assertHttpStatus(r, status.HTTP_201_CREATED)
        self.assertIsNone(r.get('Idempotency-Replayed'))
        self.assertEqual(IdempotencyKey.objects.count(), 0)

    def test_prune_expired_records(self):
        self.grant('add', 'view')
        self.client.post(
            URL, {'name': 'Old', 'slug': 'old'}, format='json', **self._key_header('old')
        )
        self.client.post(
            URL, {'name': 'New', 'slug': 'new'}, format='json', **self._key_header('new')
        )
        self.assertEqual(IdempotencyKey.objects.count(), 2)

        # Backdate one record beyond a one-hour retention window
        IdempotencyKey.objects.filter(idempotency_key='old').update(
            created=timezone.now() - timedelta(hours=2)
        )

        deleted = IdempotencyKey.objects.prune_expired(retention_seconds=3600)
        self.assertEqual(deleted, 1)
        self.assertEqual(IdempotencyKey.objects.count(), 1)
        self.assertEqual(IdempotencyKey.objects.get().idempotency_key, 'new')

        # Retention of 0 retains everything
        self.assertEqual(IdempotencyKey.objects.prune_expired(retention_seconds=0), 0)

    def test_housekeeping_job_prunes_records(self):
        self.grant('add', 'view')
        self.client.post(
            URL, {'name': 'Sweep', 'slug': 'sweep'}, format='json', **self._key_header('sweep')
        )
        IdempotencyKey.objects.update(created=timezone.now() - timedelta(days=2))

        # Bypass JobRunner.__init__ (JobLogHandler) and exercise the housekeeping method directly
        runner = SystemHousekeepingJob.__new__(SystemHousekeepingJob)
        runner.logger = __import__('logging').getLogger('test.housekeeping-idempotency')
        with override_settings(IDEMPOTENCY_KEY_RETENTION=86400):
            runner.prune_idempotency_keys()
        self.assertEqual(IdempotencyKey.objects.count(), 0)


class IdempotencyKeyConcurrencyTests(TransactionTestCase):
    """
    Concurrent same-key requests: exactly one request executes; the others block on the database
    and replay the executor's result once it commits.
    """

    def setUp(self):
        # TransactionTestCase does not call setUpTestData, so create committed fixtures in setUp().
        self.user = User.objects.create_user(username='idem-concurrent')
        self.token = Token.objects.create(user=self.user)
        self.header = {
            'HTTP_AUTHORIZATION': f'Bearer {TOKEN_PREFIX}{self.token.key}.{self.token.token}',
            'HTTP_IDEMPOTENCY_KEY': 'concurrent-key',
        }
        perm = ObjectPermission.objects.create(name='Concurrency perm', actions=['add', 'view'])
        perm.object_types.add(ObjectType.objects.get_for_model(Region))
        perm.users.add(self.user)

    def test_concurrent_requests_single_executor(self):
        n = 4
        barrier = threading.Barrier(n, timeout=60)
        payload = {'name': 'Concurrent Region', 'slug': 'concurrent-region'}
        results = []
        errors = []

        def worker():
            try:
                client = APIClient()
                # Hold every request at the start of its idempotency transaction until all are
                # present, so the INSERT race (and wait/replay) genuinely overlaps.
                patcher = patch.object(
                    IdempotencyExchange,
                    '_set_lock_timeout',
                    side_effect=lambda *args: barrier.wait(),
                )
                patcher.start()
                try:
                    response = client.post(URL, payload, format='json', **self.header)
                finally:
                    patcher.stop()
                results.append((response.status_code, response.content, response.get('Idempotency-Replayed')))
            except Exception:
                errors.append(traceback.format_exc())
            finally:
                # Release this worker thread's DB connection so the test database can be torn down
                from django.db import connection
                connection.close()

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(results), n)
        self.assertTrue(all(code == status.HTTP_201_CREATED for code, _, _ in results))
        # All clients received the same created object
        self.assertEqual(len({body for _, body, _ in results}), 1)
        self.assertEqual(Region.objects.filter(slug='concurrent-region').count(), 1)
        # Exactly one executor (no replay marker), n-1 replays
        replay_markers = [marker for _, _, marker in results]
        self.assertEqual(replay_markers.count('true'), n - 1)
        self.assertEqual([m for m in replay_markers if m != 'true'], [None])
