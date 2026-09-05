from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

__all__ = (
    'IdempotencyKey',
)


class IdempotencyKeyManager(models.Manager):

    def prune_expired(self, retention_seconds=None):
        """
        Delete completed idempotency records whose age exceeds the configured retention period.

        In-progress records are never pruned: they exist only for the duration of the request which
        created them, and are removed by transaction rollback on failure.

        Args:
            retention_seconds: Retention period in seconds. Defaults to the
                IDEMPOTENCY_KEY_RETENTION setting. A value of 0 or None retains records indefinitely.

        Returns the number of records deleted.
        """
        if retention_seconds is None:
            retention_seconds = settings.IDEMPOTENCY_KEY_RETENTION
        if not retention_seconds:
            return 0

        cutoff = timezone.now() - timedelta(seconds=retention_seconds)
        count = self.filter(
            status=IdempotencyKey.STATUS_COMPLETE,
            created__lt=cutoff,
        ).delete()[0]

        return count


class IdempotencyKey(models.Model):
    """
    Stores the outcome of a REST API write request made with an Idempotency-Key header, allowing
    subsequent retries carrying the same key (within the same authenticated user, HTTP method, and
    normalized path) to replay the original response rather than performing the write a second time.

    A row is inserted before the request is executed (status = in-progress) within the same
    transaction: the uncommitted row both serializes concurrent requests (a second insert blocks on
    the unique scope hash until the first transaction commits or rolls back) and is reclaimed
    automatically if the request fails with a server error.
    """
    _netbox_private = True

    STATUS_IN_PROGRESS = 'in-progress'
    STATUS_COMPLETE = 'complete'
    STATUS_CHOICES = (
        (STATUS_IN_PROGRESS, _('In progress')),
        (STATUS_COMPLETE, _('Complete')),
    )

    user = models.ForeignKey(
        to=settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='+',
    )
    method = models.CharField(
        max_length=10,
    )
    path = models.CharField(
        max_length=2000,
    )
    idempotency_key = models.CharField(
        max_length=255,
    )
    scope_hash = models.CharField(
        max_length=64,
        unique=True,
    )
    fingerprint = models.CharField(
        max_length=64,
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_IN_PROGRESS,
    )
    request_id = models.CharField(
        max_length=36,
        blank=True,
    )
    response_status = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
    )
    response_content_type = models.CharField(
        max_length=100,
        blank=True,
    )
    response_body = models.TextField(
        blank=True,
    )
    response_headers = models.JSONField(
        default=dict,
        blank=True,
    )
    created = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
    )

    objects = IdempotencyKeyManager()

    class Meta:
        indexes = (
            models.Index(fields=('status', 'created')),
        )
        verbose_name = _('idempotency key')
        verbose_name_plural = _('idempotency keys')

    def __str__(self):
        return f'{self.method} {self.path} [{self.idempotency_key}]'
