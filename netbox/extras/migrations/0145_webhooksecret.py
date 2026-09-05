from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('extras', '0144_customfield_status'),
    ]

    operations = [
        migrations.CreateModel(
            name='WebhookSecret',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('key_id', models.CharField(
                    help_text='A stable identifier for this secret, included in the X-Hook-Signatures request '
                              'header so receivers can select the matching key. May contain only letters, numbers, '
                              'hyphens, and underscores.',
                    max_length=100,
                    verbose_name='key ID',
                )),
                ('secret', models.CharField(
                    help_text='When provided, the request will include an HMAC hex digest of the payload body '
                              'computed with this secret as the key. The secret itself is not transmitted in the '
                              'request.',
                    max_length=255,
                    verbose_name='secret',
                )),
                ('status', models.CharField(
                    choices=[('enabled', 'Enabled'), ('disabled', 'Disabled'), ('retired', 'Retired')],
                    default='enabled',
                    help_text='Enabled secrets sign outgoing requests. Disabled secrets are retained but do not '
                              'sign. Retired secrets are permanently out of service and no longer sign new events.',
                    max_length=20,
                    verbose_name='status',
                )),
                ('is_primary', models.BooleanField(
                    default=False,
                    help_text='The primary secret signs the legacy X-Hook-Signature header. Exactly one enabled '
                              'secret must be marked primary.',
                    verbose_name='primary',
                )),
                ('webhook', models.ForeignKey(
                    on_delete=models.deletion.CASCADE,
                    related_name='secrets',
                    to='extras.webhook',
                    verbose_name='webhook',
                )),
            ],
            options={
                'ordering': ('key_id',),
                'verbose_name': 'webhook signing secret',
                'verbose_name_plural': 'webhook signing secrets',
            },
        ),
        migrations.AddConstraint(
            model_name='webhooksecret',
            constraint=models.UniqueConstraint(
                fields=('webhook', 'key_id'),
                name='extras_webhooksecret_unique_webhook_key_id',
            ),
        ),
        migrations.AddConstraint(
            model_name='webhooksecret',
            constraint=models.UniqueConstraint(
                condition=models.Q(is_primary=True),
                fields=('webhook',),
                name='extras_webhooksecret_one_primary_per_webhook',
            ),
        ),
    ]
