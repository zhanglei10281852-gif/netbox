from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from rest_framework import serializers

from core.models import ObjectType
from extras.choices import *
from extras.models import EventRule, Webhook, validate_signing_secrets
from netbox.api.fields import ChoiceField, ContentTypeField
from netbox.api.gfk_fields import GFKSerializerField
from netbox.api.serializers import NetBoxModelSerializer
from netbox.event_rules import get_event_rule_action_choices
from users.api.serializers_.mixins import OwnerMixin

__all__ = (
    'EventRuleSerializer',
    'WebhookSerializer',
)


#
# Event Rules
#

class EventRuleSerializer(OwnerMixin, NetBoxModelSerializer):
    object_types = ContentTypeField(
        queryset=ObjectType.objects.with_feature('event_rules'),
        many=True
    )
    action_type = ChoiceField(choices=[])  # Choices are set by get_fields()
    action_object_type = ContentTypeField(
        queryset=ObjectType.objects.all(),
        required=False,
        allow_null=True,
    )
    action_object = GFKSerializerField(read_only=True)
    action_is_available = serializers.BooleanField(read_only=True)

    class Meta:
        model = EventRule
        fields = [
            'id', 'url', 'display_url', 'display', 'object_types', 'name', 'enabled', 'event_types', 'conditions',
            'action_type', 'action_object_type', 'action_object_id', 'action_object', 'action_is_available',
            'description', 'custom_fields', 'owner', 'tags', 'created', 'last_updated',
        ]
        brief_fields = ('id', 'url', 'display', 'name', 'description')

    def get_fields(self):
        fields = super().get_fields()

        # Rebuild action_type from the live registry on each instantiation to ensure all registered
        # actions are captured as choices.
        if 'action_type' in fields:
            fields['action_type'] = ChoiceField(choices=get_event_rule_action_choices())

        return fields


#
# Webhooks
#

class WebhookSecretSerializer(serializers.Serializer):
    """
    A single signing secret belonging to a webhook. Managed inline via the webhook endpoint so
    that the whole set of keys (including primary designation) is validated and saved atomically.
    """
    key_id = serializers.CharField(
        max_length=100,
        help_text="Stable identifier of this key, included in the X-Hook-Signatures request header."
    )
    secret = serializers.CharField(
        max_length=255,
        help_text="HMAC key used to sign the request body."
    )
    status = ChoiceField(
        choices=WebhookSecretStatusChoices,
        default=WebhookSecretStatusChoices.STATUS_ENABLED,
        required=False,
        help_text="Enabled keys sign events; disabled keys do not; retired keys are terminal and never sign new events."
    )
    is_primary = serializers.BooleanField(
        default=False,
        required=False,
        help_text="When true, this key also signs the legacy X-Hook-Signature header. Exactly one enabled key "
                  "must be primary."
    )

    def validate_key_id(self, value):
        if not value:
            raise serializers.ValidationError('Key ID is required.')
        return value

    def validate_secret(self, value):
        if not value:
            raise serializers.ValidationError('Secret value is required.')
        return value


class WebhookSerializer(OwnerMixin, NetBoxModelSerializer):
    secrets = WebhookSecretSerializer(
        many=True,
        required=False,
        help_text="Signing secrets for this webhook. The set is replaced in full whenever provided. "
                  "Exactly one enabled secret must be marked is_primary.",
    )

    class Meta:
        model = Webhook
        fields = [
            'id', 'url', 'display_url', 'display', 'name', 'description', 'payload_url', 'http_method',
            'http_content_type', 'additional_headers', 'body_template', 'secrets', 'ssl_verification', 'ca_file_path',
            'timeout', 'custom_fields', 'owner', 'tags', 'created', 'last_updated',
        ]
        brief_fields = ('id', 'url', 'display', 'name', 'description')

    def _sync_secrets(self, instance, secrets_data):
        """
        Validate and reconcile the supplied signing secrets onto the webhook. Raises a DRF
        ValidationError (keyed under 'secrets') when the desired set is incomplete, leaving the
        database untouched (the enclosing transaction is rolled back).
        """
        keys = [
            {
                'key_id': item['key_id'],
                'secret': item['secret'],
                'status': item.get('status', WebhookSecretStatusChoices.STATUS_ENABLED),
                'is_primary': item.get('is_primary', False),
            }
            for item in secrets_data
        ]
        try:
            validate_signing_secrets(instance, keys)
        except DjangoValidationError as e:
            raise serializers.ValidationError(e.message_dict if hasattr(e, 'message_dict') else e.messages)
        instance.sync_secrets(keys)

    def create(self, validated_data):
        secrets_data = validated_data.pop('secrets', None)
        with transaction.atomic():
            instance = super().create(validated_data)
            if secrets_data is not None:
                self._sync_secrets(instance, secrets_data)
        return instance

    def update(self, instance, validated_data):
        secrets_data = validated_data.pop('secrets', None)
        with transaction.atomic():
            instance = super().update(instance, validated_data)
            if secrets_data is not None:
                self._sync_secrets(instance, secrets_data)
        return instance
