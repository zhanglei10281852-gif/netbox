from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('extras', '0146_webhook_secrets_populate'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='webhook',
            name='secret',
        ),
    ]
