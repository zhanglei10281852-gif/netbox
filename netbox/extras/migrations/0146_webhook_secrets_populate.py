from django.db import migrations

# Stable key_id assigned to the single secret migrated from the legacy Webhook.secret field.
DEFAULT_KEY_ID = 'default'
STATUS_ENABLED = 'enabled'


def populate_webhook_secrets(apps, schema_editor):
    """
    Migrate each webhook's legacy single `secret` value to a WebhookSecret row.

    The migrated secret is enabled and marked primary, so the primary key's HMAC signature
    (and therefore the legacy X-Hook-Signature header) is byte-for-byte identical to what the
    webhook produced before the upgrade. Webhooks without a secret remain unsigned.
    """
    Webhook = apps.get_model('extras', 'Webhook')
    WebhookSecret = apps.get_model('extras', 'WebhookSecret')

    for webhook in Webhook.objects.exclude(secret=''):
        WebhookSecret.objects.get_or_create(
            webhook=webhook,
            key_id=DEFAULT_KEY_ID,
            defaults={
                'secret': webhook.secret,
                'status': STATUS_ENABLED,
                'is_primary': True,
            },
        )


def depopulate_webhook_secrets(apps, schema_editor):
    """
    Best-effort reverse: copy the migrated primary secret back onto the webhook. WebhookSecret
    rows are removed by the reverse of the migration which created the table.
    """
    Webhook = apps.get_model('extras', 'Webhook')

    for webhook in Webhook.objects.all():
        secret = webhook.secrets.filter(key_id=DEFAULT_KEY_ID).first()
        if secret is not None:
            webhook.secret = secret.secret
            webhook.save(update_fields=['secret'])


class Migration(migrations.Migration):

    dependencies = [
        ('extras', '0145_webhooksecret'),
    ]

    operations = [
        migrations.RunPython(code=populate_webhook_secrets, reverse_code=depopulate_webhook_secrets),
    ]
