"""
Idempotency-Key support for unsafe REST API requests.

An ``Idempotency-Key`` header on a POST, PUT, PATCH or DELETE request identifies a single logical
operation within the scope of the authenticated user, HTTP method, and normalized request path.
The first request to carry a given key executes normally and its final response (status code,
rendered body, and a whitelist of response headers) is recorded alongside a semantic fingerprint
of the request (its raw body and the query parameters which affect write semantics). Subsequent
requests with the same scope, key, and fingerprint replay the stored response without executing
the handler a second time: no additional database writes, change log entries, event rules, or
background jobs result. A key reused with a different request semantics is rejected with HTTP 409.

Concurrency and failure handling rely entirely on the database:

* The executor inserts the record inside the same transaction that wraps the request, so the row
  is uncommitted while the request is in flight. A concurrent request attempting the same insert
  blocks on the unique scope-hash index until the first transaction ends:
    - commits: the waiter loses the insert race, reads the completed record, and replays it;
    - rolls back (server error, process crash): the waiter's insert succeeds and it becomes
      the executor.
  A bounded ``lock_timeout`` caps how long a waiter blocks before being told (HTTP 409) to retry.
* A 5xx response (or an unhandled exception) rolls the executor's transaction back, discarding
  the placeholder, so a later retry with the same key re-executes the request.
"""
import hashlib
from urllib.parse import urlencode

from django.conf import settings
from django.db import connections, router, transaction
from django.http import HttpResponse
from django.utils.translation import gettext_lazy as _
from rest_framework.exceptions import ValidationError

from core.models import IdempotencyKey
from core.signals import clear_events
from netbox.api.exceptions import IdempotencyKeyConflict, IdempotencyKeyInProgress

__all__ = (
    'IDEMPOTENCY_KEY_HEADER',
    'IDEMPOTENCY_REPLAY_HEADER',
    'IdempotencyExchange',
    'idempotency_key_postprocessing_hook',
)

IDEMPOTENCY_KEY_HEADER = 'Idempotency-Key'
IDEMPOTENCY_REPLAY_HEADER = 'Idempotency-Replayed'

UNSAFE_METHODS = frozenset(('POST', 'PUT', 'PATCH', 'DELETE'))

# Query parameters which affect only how the response is rendered, never the write performed.
# Differences in these do not constitute a different request for idempotency purposes.
RESPONSE_ONLY_QUERY_PARAMS = frozenset(('brief', 'fields', 'format', 'omit'))

# Response headers persisted with the recorded response and replayed to retries.
REPLAYABLE_RESPONSE_HEADERS = ('ETag', 'Location')

MAX_KEY_LENGTH = 255

# PostgreSQL SQLSTATE for lock_not_available (SET LOCAL lock_timeout elapsing) and unique_violation.
SQLSTATE_LOCK_NOT_AVAILABLE = '55P03'


def normalize_path(path):
    """Return the request path stripped of its trailing slash, so /sites/ and /sites match."""
    return path.rstrip('/') or path


def compute_scope_hash(user_pk, method, path, key):
    """Return the hash identifying an idempotency scope: (user, method, normalized path, key)."""
    scope = f'{user_pk}\n{method}\n{normalize_path(path)}\n{key}'
    return hashlib.sha256(scope.encode('utf-8')).hexdigest()


def compute_fingerprint(request):
    """
    Return a hash of the request's write semantics: its raw body plus every query parameter
    except the response-shaping ones (brief/fields/format/omit). Parameters which alter what the
    write does (e.g. ``background``) are part of the fingerprint, so reusing a key across them is
    reported as a conflict.
    """
    # Access the underlying Django request's body: Django caches the raw bytes and rewinds the
    # stream, so DRF parsers still see the full payload when the handler runs.
    body = request._request.body or b''

    params = []
    for name, values in sorted(request.query_params.lists()):
        if name in RESPONSE_ONLY_QUERY_PARAMS:
            continue
        params.extend((name, value) for value in values)
    query = urlencode(params)

    digest = hashlib.sha256()
    digest.update(body)
    digest.update(b'\n')
    digest.update(query.encode('utf-8'))
    return digest.hexdigest()


def validate_key(header_value):
    """
    Validate the Idempotency-Key header value. Returns the key, or raises ValidationError (400)
    for an empty, overlong, or malformed value. Returns None when no header was sent.
    """
    if header_value is None:
        return None
    key = header_value
    if (
        not key.strip()
        or len(key) > MAX_KEY_LENGTH
        or any(ord(char) < 0x20 and char != '\t' for char in key)
    ):
        raise ValidationError(
            _('The {header} header must contain between 1 and {max_length} printable characters.').format(
                header=IDEMPOTENCY_KEY_HEADER,
                max_length=MAX_KEY_LENGTH,
            )
        )
    return key


def _sqlstate(exc):
    """Walk an exception chain looking for a PostgreSQL SQLSTATE code (psycopg exposes pgcode/sqlstate)."""
    seen = exc
    while seen is not None:
        code = getattr(seen, 'pgcode', None) or getattr(seen, 'sqlstate', None)
        if code:
            return code
        seen = seen.__cause__ or seen.__context__
    return None


class IdempotencyExchange:
    """
    Orchestrates a single idempotent write: either this request is the executor (it records its
    outcome as it completes), or it replays the outcome recorded by the request that won the
    scope race.
    """

    def __init__(self, request, key):
        self.request = request
        self.key = key
        self.fingerprint = compute_fingerprint(request)
        self.scope_hash = compute_scope_hash(request.user.pk, request.method, request.path, key)
        self.using = router.db_for_write(IdempotencyKey)
        self.record = None
        self.acquired = False

    @classmethod
    def engage(cls, request, method_not_allowed):
        """
        Return an exchange for the request if idempotency handling applies, else None. Idempotency
        engages only for authenticated unsafe-method requests carrying a well-formed key on a
        handler that actually performs work (405 handlers are excluded). Authentication, permission,
        and throttle checks have already run by this point, so their rejections are unaffected.
        """
        if not getattr(settings, 'IDEMPOTENCY_KEY_ENABLED', True):
            return None
        if request.method not in UNSAFE_METHODS:
            return None
        if method_not_allowed:
            return None
        if not request.user.is_authenticated:
            return None
        key = validate_key(request.META.get('HTTP_IDEMPOTENCY_KEY'))
        if key is None:
            return None
        return cls(request, key)

    def run(self, view, handler, args, kwargs):
        """
        Execute the handler as the idempotency executor, or return a stored replay response as a
        waiter. May raise IdempotencyKeyConflict / IdempotencyKeyInProgress (DRF 409 responses).
        """
        request = self.request
        try:
            with transaction.atomic(using=self.using):
                self._set_lock_timeout()
                try:
                    self.record = self._insert_placeholder()
                except Exception as exc:
                    # A blocked insert that timed out rather than winning or losing the race means
                    # the executor is still working; tell the client to retry shortly.
                    if _sqlstate(exc) == SQLSTATE_LOCK_NOT_AVAILABLE:
                        raise IdempotencyKeyInProgress()
                    raise
                self.acquired = True
                self._reset_lock_timeout()

                try:
                    response = handler(request, *args, **kwargs)
                except Exception as exc:
                    # Reuse the view's own exception translation so the stored response matches what
                    # a non-idempotent request would have returned (ProtectedError -> 409, etc.).
                    response = self._translate_exception(view, exc)

                response = view.finalize_response(request, response, *args, **kwargs)
                # Render now (DRF Responses only; plain Django Responses already carry content) so
                # the stored body is byte-for-byte what the first client received. finalize_response
                # runs again in dispatch() on the way out; it is idempotent.
                if hasattr(response, 'render'):
                    response.render()

                if getattr(response, 'streaming', False):
                    # Streaming bodies cannot be captured for replay: release the placeholder and
                    # the transaction rather than recording a result that could not be replayed.
                    transaction.set_rollback(True, using=self.using)
                elif response.status_code >= 500:
                    # Server-side failure: discard the placeholder (and any uncommitted writes) so a
                    # retry re-executes instead of being blocked by an in-progress/error record.
                    transaction.set_rollback(True, using=self.using)
                    # Drop any events queued by the failed write, mirroring discard_events_on_rollback.
                    clear_events.send(sender=view)
                else:
                    self._store(response)
        except Exception as exc:
            # The insert lost the race to a committed record: this request is a waiter.
            # Any other failure (incl. a unique violation from the handler itself) propagates.
            if not self.acquired and _sqlstate(exc) == '23505':
                return self._replay_or_conflict()
            raise

        return response

    def _insert_placeholder(self):
        return IdempotencyKey.objects.using(self.using).create(
            user_id=self.request.user.pk,
            method=self.request.method,
            path=normalize_path(self.request.path),
            idempotency_key=self.key,
            scope_hash=self.scope_hash,
            fingerprint=self.fingerprint,
            request_id=str(getattr(self.request, 'id', '') or ''),
        )

    def _translate_exception(self, view, exc):
        """
        Translate an exception raised by the handler into a Response, mirroring the view's normal
        dispatch behavior: NetBox's exception_to_response() first (ProtectedError/RestrictedError ->
        409, AbortRequest -> 400), then DRF's handle_exception() (validation, permissions, 404, ...).
        Exceptions neither can handle are re-raised (and roll the executor transaction back).
        """
        translator = getattr(view, 'exception_to_response', None)
        if translator is not None:
            try:
                translated = translator(exc)
            except Exception:
                translated = None
            if translated is not None:
                return translated
        return view.handle_exception(exc)

    def _store(self, response):
        """Persist the executor's final response on the placeholder record."""
        headers = {
            name: response[name]
            for name in REPLAYABLE_RESPONSE_HEADERS
            if name in response
        }
        body = response.content or b''
        self.record.status = IdempotencyKey.STATUS_COMPLETE
        self.record.response_status = response.status_code
        self.record.response_content_type = response.get('Content-Type', '') or ''
        self.record.response_body = body.decode('utf-8')
        self.record.response_headers = headers
        self.record.save(using=self.using)

    def _replay_or_conflict(self):
        """
        Waiter path: another request committed a record for this scope. Replay it when the
        fingerprint matches, otherwise reject the request as a conflict.
        """
        record = IdempotencyKey.objects.using(self.using).filter(
            scope_hash=self.scope_hash
        ).first()
        if record is None:
            # The executor rolled back without persisting a record (a record visible to a unique
            # violation should normally be committed); ask the client to retry.
            raise IdempotencyKeyInProgress()
        if record.fingerprint != self.fingerprint:
            raise IdempotencyKeyConflict()
        if record.status != IdempotencyKey.STATUS_COMPLETE or record.response_status is None:
            raise IdempotencyKeyInProgress()
        return self._build_replay(record)

    @staticmethod
    def _build_replay(record):
        """Construct the stored response verbatim, flagged as a replay."""
        response = HttpResponse(
            content=record.response_body or '',
            content_type=record.response_content_type or None,
            status=record.response_status,
        )
        if not record.response_content_type and 'Content-Type' in response:
            # DRF omits Content-Type for empty responses (e.g. 204 No Content).
            del response['Content-Type']
        for name, value in record.response_headers.items():
            response[name] = value
        response[IDEMPOTENCY_REPLAY_HEADER] = 'true'
        return response

    def _set_lock_timeout(self):
        timeout_ms = max(1, int(getattr(settings, 'IDEMPOTENCY_KEY_LOCK_TIMEOUT', 60))) * 1000
        with connections[self.using].cursor() as cursor:
            cursor.execute(f"SET LOCAL lock_timeout = '{timeout_ms}ms'")

    def _reset_lock_timeout(self):
        with connections[self.using].cursor() as cursor:
            cursor.execute("SET LOCAL lock_timeout = '0'")


def idempotency_key_postprocessing_hook(result, generator, **kwargs):
    """
    drf-spectacular postprocessing hook: advertise the Idempotency-Key request header on every
    unsafe (POST/PUT/PATCH/DELETE) operation.
    """
    parameter = {
        'name': IDEMPOTENCY_KEY_HEADER,
        'in': 'header',
        'required': False,
        'description': (
            'Idempotency key for safe retries. Repeating a write request with the same key '
            '(within the same authenticated user and request path) replays the original response '
            'instead of performing the write again. A key reused with a different request payload '
            'returns HTTP 409.'
        ),
        'schema': {'type': 'string', 'maxLength': MAX_KEY_LENGTH},
    }

    for path_spec in result.get('paths', {}).values():
        if not isinstance(path_spec, dict):
            continue
        for method, operation in path_spec.items():
            if method.upper() not in UNSAFE_METHODS or not isinstance(operation, dict):
                continue
            parameters = operation.setdefault('parameters', [])
            if any(
                isinstance(param, dict) and param.get('in') == 'header'
                and param.get('name') == IDEMPOTENCY_KEY_HEADER
                for param in parameters
            ):
                continue
            parameters.append(parameter)

    return result
