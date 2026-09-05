import json
import re
import urllib.parse
from pathlib import Path

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey, GenericRelation
from django.contrib.postgres.fields import ArrayField
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape
from django.utils.safestring import mark_safe
from django.utils.text import format_lazy
from django.utils.translation import gettext_lazy as _
from rest_framework.utils.encoders import JSONEncoder

from extras.choices import *
from extras.conditions import ConditionSet, InvalidCondition
from extras.constants import *
from extras.models.mixins import RenderTemplateMixin
from extras.querysets import SharedObjectQuerySet
from extras.utils import image_upload
from netbox.config import get_config
from netbox.event_rules import get_event_rule_action, get_event_rule_action_choices
from netbox.events import get_event_type_choices
from netbox.models import ChangeLoggedModel
from netbox.models.features import (
    CloningMixin,
    CustomFieldsMixin,
    CustomLinksMixin,
    ExportTemplatesMixin,
    SyncedDataMixin,
    TagsMixin,
    has_feature,
)
from netbox.models.mixins import OwnerMixin
from netbox.settings_utils import parse_job_timeout
from utilities.html import clean_html
from utilities.jinja2 import JINJA2_TEMPLATE_RE, render_jinja2, sanitize_http_header, validate_jinja2_syntax
from utilities.querydict import dict_to_querydict
from utilities.querysets import RestrictedQuerySet
from utilities.tables import get_table_for_model

__all__ = (
    'Bookmark',
    'CustomLink',
    'EventRule',
    'ExportTemplate',
    'ImageAttachment',
    'JournalEntry',
    'SavedFilter',
    'TableConfig',
    'Webhook',
    'WebhookSecret',
    'validate_signing_secrets',
)

# Matches a literal URL scheme (RFC 3986), independent of urlsplit()'s netloc parsing -- which can
# raise ValueError on a malformed host -- so a payload_url's scheme can always be read even when
# its host is templated or malformed.
LITERAL_SCHEME_RE = re.compile(r'^([a-zA-Z][a-zA-Z0-9+.-]*):')


class EventRule(CustomFieldsMixin, ExportTemplatesMixin, OwnerMixin, TagsMixin, ChangeLoggedModel):
    """
    An EventRule defines an action to be taken automatically in response to a specific set of events, such as when a
    specific type of object is created, modified, or deleted. The action to be taken might entail transmitting a
    webhook or executing a custom script.
    """
    object_types = models.ManyToManyField(
        to='contenttypes.ContentType',
        related_name='event_rules',
        verbose_name=_('object types'),
        help_text=_("The object(s) to which this rule applies.")
    )
    name = models.CharField(
        verbose_name=_('name'),
        max_length=150,
        unique=True
    )
    description = models.CharField(
        verbose_name=_('description'),
        max_length=200,
        blank=True
    )
    event_types = ArrayField(
        base_field=models.CharField(max_length=50, choices=get_event_type_choices),
        help_text=_("The types of event which will trigger this rule.")
    )
    enabled = models.BooleanField(
        verbose_name=_('enabled'),
        default=True
    )
    conditions = models.JSONField(
        verbose_name=_('conditions'),
        blank=True,
        null=True,
        help_text=_("A set of conditions which determine whether the event will be generated.")
    )

    # Action to take
    action_type = models.CharField(
        max_length=100,
        # Bare callable, re-evaluated fresh on each access via Django's CallableChoiceIterator,
        # so a plugin action registered after this module was first imported is still reflected.
        choices=get_event_rule_action_choices,
        default=EventRuleActionChoices.WEBHOOK,
        verbose_name=_('action type')
    )
    action_object_type = models.ForeignKey(
        to='contenttypes.ContentType',
        related_name='eventrule_actions',
        on_delete=models.CASCADE,
        blank=True,
        null=True,
    )
    action_object_id = models.PositiveBigIntegerField(
        blank=True,
        null=True
    )
    action_object = GenericForeignKey(
        ct_field='action_object_type',
        fk_field='action_object_id'
    )
    action_data = models.JSONField(
        verbose_name=_('data'),
        blank=True,
        null=True,
        help_text=_("Additional data to pass to the action object")
    )
    comments = models.TextField(
        verbose_name=_('comments'),
        blank=True
    )

    class Meta:
        ordering = ('name',)
        indexes = (
            models.Index(fields=('action_object_type', 'action_object_id')),
        )
        verbose_name = _('event rule')
        verbose_name_plural = _('event rules')

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse('extras:eventrule', args=[self.pk])

    @property
    def action_provider(self):
        """
        Return the registered EventRuleAction instance for this rule's action_type, or None if it
        is not currently registered (e.g. the providing plugin is not installed).
        """
        return get_event_rule_action(self.action_type)

    @property
    def action_is_available(self):
        return self.action_provider is not None

    def get_action_type_display(self):
        if action := self.action_provider:
            return action.label
        return _('{slug} (unavailable)').format(slug=self.action_type)

    def get_action_type_color(self):
        return None if self.action_is_available else 'red'

    def clean(self):
        super().clean()

        # Validate that any conditions are in the correct format
        if self.conditions:
            try:
                ConditionSet(self.conditions)
            except ValueError as e:
                raise ValidationError({'conditions': e})

        # action_data must be a JSON object (or null)
        if self.action_data is not None and not isinstance(self.action_data, dict):
            raise ValidationError({'action_data': _('Action data must be a JSON object or null.')})

        # action_type's own validity is already enforced by the field's dynamic choices= (Field.
        # validate(), earlier in full_clean()); guard here only in case clean() ran standalone.
        if self.action_is_available:
            self.action_provider._validate(action_object=self.action_object, action_data=self.action_data)

    def eval_conditions(self, data):
        """
        Test whether the given data meets the conditions of the event rule (if any). Return True
        if met or no conditions are specified.
        """
        if not self.conditions:
            return True

        logger = logging.getLogger('netbox.event_rules')

        try:
            result = ConditionSet(self.conditions).eval(data)
            logger.debug(f'{self.name}: Evaluated as {result}')
            return result
        except InvalidCondition as e:
            logger.error(f"{self.name}: Evaluation failed. {e}")
            return False


class Webhook(CustomFieldsMixin, ExportTemplatesMixin, TagsMixin, OwnerMixin, ChangeLoggedModel):
    """
    A Webhook defines a request that will be sent to a remote application when an object is created, updated, and/or
    delete in NetBox. The request will contain a representation of the object, which the remote application can act on.
    Each Webhook can be limited to firing only on certain actions or certain object types.
    """
    name = models.CharField(
        verbose_name=_('name'),
        max_length=150,
        unique=True
    )
    description = models.CharField(
        verbose_name=_('description'),
        max_length=200,
        blank=True
    )
    payload_url = models.CharField(
        max_length=500,
        verbose_name=_('URL'),
        help_text=_(
            "This URL will be called using the HTTP method defined when the webhook is called. Must be "
            "http:// or https://. Jinja2 template processing is supported (with the same context as the "
            "request body) for part or all of the URL."
        )
    )
    http_method = models.CharField(
        max_length=30,
        choices=WebhookHttpMethodChoices,
        default=WebhookHttpMethodChoices.METHOD_POST,
        verbose_name=_('HTTP method')
    )
    http_content_type = models.CharField(
        max_length=100,
        default=HTTP_CONTENT_TYPE_JSON,
        verbose_name=_('HTTP content type'),
        help_text=_(
            'The complete list of official content types is available '
            '<a href="https://www.iana.org/assignments/media-types/media-types.xhtml">here</a>.'
        )
    )
    additional_headers = models.TextField(
        verbose_name=_('additional headers'),
        blank=True,
        help_text=_(
            "User-supplied HTTP headers to be sent with the request in addition to the HTTP content type. Headers "
            "should be defined in the format <code>Name: Value</code>. Jinja2 template processing is supported with "
            "the same context as the request body (below). When interpolating untrusted data (such as object "
            "attributes) into a header value, apply the <code>header_safe</code> filter to guard against HTTP header "
            "injection, e.g. <code>X-Object: {{ data.name | header_safe }}</code>."
        )
    )
    body_template = models.TextField(
        verbose_name=_('body template'),
        blank=True,
        help_text=_(
            "Jinja2 template for a custom request body. If blank, a JSON object representing the change will be "
            "included. Available context data includes: <code>event</code>, <code>model</code>, "
            "<code>timestamp</code>, <code>request</code>, and <code>data</code>."
        )
    )
    ssl_verification = models.BooleanField(
        default=True,
        verbose_name=_('SSL verification'),
        help_text=_("Enable SSL certificate verification. Disable with caution!")
    )
    ca_file_path = models.CharField(
        max_length=4096,
        null=True,
        blank=True,
        verbose_name=_('CA File Path'),
        help_text=_(
            "The specific CA certificate file to use for SSL verification. Leave blank to use the system defaults."
        )
    )
    timeout = models.PositiveSmallIntegerField(
        verbose_name=_('timeout'),
        null=True,
        blank=True,
        validators=(
            MinValueValidator(1),
            MaxValueValidator(3600),
        ),
        help_text=format_lazy(
            _(
                "The maximum time (in seconds) to wait for a response before failing the request. Leave blank to use "
                "the system default ({default_timeout} seconds)."
            ),
            default_timeout=settings.WEBHOOK_DEFAULT_TIMEOUT
        )
    )
    events = GenericRelation(
        EventRule,
        content_type_field='action_object_type',
        object_id_field='action_object_id'
    )

    class Meta:
        ordering = ('name',)
        verbose_name = _('webhook')
        verbose_name_plural = _('webhooks')

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse('extras:webhook', args=[self.pk])

    @property
    def docs_url(self):
        return f'{settings.STATIC_URL}docs/models/extras/webhook/'

    def clean(self):
        super().clean()

        errors = {}

        # CA file path requires SSL verification enabled
        if not self.ssl_verification and self.ca_file_path:
            errors['ca_file_path'] = _('Do not specify a CA certificate file if SSL verification is disabled.')

        # payload_url may be a literal URL or a Jinja2 template (see its help_text). Skipped when
        # blank; clean_fields() already flags that.
        if self.payload_url:
            if JINJA2_TEMPLATE_RE.search(self.payload_url):
                # A literal, disallowed scheme (e.g. "file://") can never resolve no matter what
                # else in the value is templated; anything else is checked for template syntax
                # only, since its rendered result isn't known here.
                match = LITERAL_SCHEME_RE.match(self.payload_url)
                if match and match.group(1).lower() not in ('http', 'https'):
                    errors['payload_url'] = _("Enter a valid URL, beginning with http:// or https://.")
                else:
                    try:
                        validate_jinja2_syntax(self.payload_url)
                    except ValidationError as e:
                        errors['payload_url'] = e
            else:
                # Fully literal -- validate directly rather than via URLValidator, which rejects
                # single-label and underscore hosts that `requests` accepts fine. urlsplit() can
                # raise ValueError for a malformed netloc (e.g. an unbalanced IPv6 bracket).
                try:
                    scheme, netloc = urllib.parse.urlsplit(self.payload_url)[:2]
                except ValueError:
                    scheme, netloc = '', ''
                if scheme not in ('http', 'https') or not netloc:
                    errors['payload_url'] = _("Enter a valid URL, beginning with http:// or https://.")

        if errors:
            raise ValidationError(errors)

        # A timeout which meets or exceeds the background job timeout leaves no room for the request's own timeout
        # to apply: the worker will terminate the job first. (Staying below the job timeout does not guarantee that
        # the request times out on its own, as the timeout applies separately to connecting and to reading data.)
        job_timeout = parse_job_timeout(settings.RQ_DEFAULT_TIMEOUT)
        if self.timeout is not None and job_timeout is not None and self.timeout >= job_timeout:
            raise ValidationError({
                'timeout': _(
                    "Timeout must be less than the background job timeout ({timeout} seconds)."
                ).format(timeout=job_timeout)
            })

    def render_headers(self, context):
        """
        Render additional_headers and return a dict of Header: Value pairs.
        """
        if not self.additional_headers:
            return {}
        ret = {}
        # Expose the `header_safe` filter so template authors can sanitize interpolated values (e.g. user-controlled
        # object data) against HTTP header (CR/LF) injection. See utilities.jinja2.sanitize_http_header.
        data = render_jinja2(self.additional_headers, context, filters={'header_safe': sanitize_http_header})
        for line in data.splitlines():
            if ':' not in line:
                continue
            header, value = line.split(':', 1)
            ret[header.strip()] = value.strip()
        return ret

    def render_body(self, context):
        """
        Render the body template, if defined. Otherwise, jump the context as a JSON object.
        """
        if self.body_template:
            return render_jinja2(self.body_template, context)
        return json.dumps(context, cls=JSONEncoder)

    def render_payload_url(self, context):
        """
        Render the payload URL.
        """
        return render_jinja2(self.payload_url, context)

    def get_signing_keys_snapshot(self):
        """
        Return the set of signing keys to use for an event enqueued *now*, as a list of plain
        dicts suitable for pickling into the background job's parameters:

            [{'key_id': <str>, 'secret': <str>, 'is_primary': <bool>}, ...]

        Only enabled secrets sign events; disabled and retired secrets are excluded. This
        snapshot is fixed at enqueue time, so subsequent configuration changes (and job
        retries) cannot alter the signature identity of an already-queued event.
        """
        return [
            {
                'key_id': secret.key_id,
                'secret': secret.secret,
                'is_primary': secret.is_primary,
            }
            for secret in self.secrets.filter(
                status=WebhookSecretStatusChoices.STATUS_ENABLED
            ).order_by('key_id')
        ]

    def sync_secrets(self, secrets):
        """
        Reconcile this webhook's signing secrets against the supplied list of dicts (each with
        keys: key_id, secret, status, is_primary). Secrets whose key_id is absent from the list
        are deleted. The data must have been validated beforehand via validate_signing_secrets().

        Must be called within a transaction. is_primary is cleared on all rows first to avoid
        violating the single-primary unique constraint while primary ownership is moving.
        """
        self.secrets.update(is_primary=False)
        seen = set()
        for data in secrets:
            seen.add(data['key_id'])
            WebhookSecret.objects.update_or_create(
                webhook=self,
                key_id=data['key_id'],
                defaults={
                    'secret': data['secret'],
                    'status': data['status'],
                    'is_primary': data['is_primary'],
                },
            )
        self.secrets.exclude(key_id__in=seen).delete()


# Signing-secret key IDs appear verbatim in the X-Hook-Signatures header, so restrict them to a
# character set which keeps the "key_id=hexdigest" header format unambiguous.
WEBHOOK_KEY_ID_RE = re.compile(r'^[A-Za-z0-9_-]+$')


def validate_signing_secrets(webhook, secrets):
    """
    Validate a desired set of signing secrets for a webhook.

    :param webhook: The Webhook instance (may be unsaved for new objects, in which case no
        persisted secrets exist yet).
    :param secrets: A list of dicts, each with keys key_id, secret, status, is_primary.
    :raises ValidationError: keyed under 'secrets', listing every problem found.

    Enforced invariants:
      * key_id is required, unique within the webhook, and limited to [A-Za-z0-9_-].
      * secret is required for every key.
      * When any secrets exist, exactly one is marked primary, and it must be enabled.
      * A retired secret is terminal: it may be left as-is or deleted, but not re-enabled or
        modified.
    """
    errors = []

    existing = {}
    if webhook.pk:
        existing = {s.key_id: s for s in WebhookSecret.objects.filter(webhook=webhook)}

    seen = set()
    primary_count = 0
    for index, data in enumerate(secrets, start=1):
        key_id = (data.get('key_id') or '').strip()
        secret = (data.get('secret') or '').strip()
        status = data.get('status') or WebhookSecretStatusChoices.STATUS_ENABLED
        is_primary = bool(data.get('is_primary'))

        label = key_id or _('#{index}').format(index=index)

        if not key_id:
            errors.append(_('A key ID is required for each signing secret.'))
        elif not WEBHOOK_KEY_ID_RE.match(key_id):
            errors.append(_(
                "Signing secret key ID '{key_id}' is invalid: only letters, numbers, hyphens, "
                "and underscores are allowed."
            ).format(key_id=key_id))
        elif key_id in seen:
            errors.append(_("Duplicate signing secret key ID '{key_id}'.").format(key_id=key_id))
        seen.add(key_id)

        if not secret:
            errors.append(_("Signing secret '{key_id}' requires a secret value.").format(key_id=label))
        elif len(secret) > 255:
            errors.append(_("Signing secret '{key_id}' must be at most 255 characters.").format(key_id=label))

        if status not in WebhookSecretStatusChoices.values():
            errors.append(_("Signing secret '{key_id}' has an invalid status.").format(key_id=label))

        if is_primary:
            primary_count += 1
            if status == WebhookSecretStatusChoices.STATUS_RETIRED:
                errors.append(_(
                    "Signing secret '{key_id}' cannot be retired while it is the primary key: "
                    "designate another secret as primary first."
                ).format(key_id=label))
            elif status != WebhookSecretStatusChoices.STATUS_ENABLED:
                errors.append(_("The primary signing secret ('{key_id}') must be enabled.").format(key_id=label))

        # A retired secret is terminal: it may be left unchanged or deleted, but never modified.
        persisted = existing.get(key_id)
        if persisted is not None and persisted.status == WebhookSecretStatusChoices.STATUS_RETIRED:
            if status != WebhookSecretStatusChoices.STATUS_RETIRED:
                errors.append(_(
                    "Signing secret '{key_id}' is retired and cannot be re-enabled: delete it or "
                    "leave it retired."
                ).format(key_id=key_id))
            elif secret and secret != persisted.secret:
                errors.append(_(
                    "Signing secret '{key_id}' is retired and its secret value cannot be modified."
                ).format(key_id=key_id))

    if secrets and primary_count == 0:
        errors.append(_('A primary signing secret is required when any signing secrets are defined.'))
    elif primary_count > 1:
        errors.append(_('Only one signing secret may be marked as primary.'))

    if errors:
        raise ValidationError({'secrets': errors})


class WebhookSecret(models.Model):
    """
    A signing secret for a Webhook. Each webhook may hold multiple secrets identified by a stable
    key_id to support key rotation with an overlap period during which receivers accept both old
    and new keys. Exactly one enabled secret is designated primary.

    Secrets are not change-logged (to avoid recording key material in change history) and are
    managed through their parent webhook (UI form / REST API), not as standalone objects.
    """
    webhook = models.ForeignKey(
        to=Webhook,
        on_delete=models.CASCADE,
        related_name='secrets',
        verbose_name=_('webhook'),
    )
    key_id = models.CharField(
        max_length=100,
        verbose_name=_('key ID'),
        help_text=_(
            "A stable identifier for this secret, included in the <code>X-Hook-Signatures</code> "
            "request header so receivers can select the matching key. May contain only letters, "
            "numbers, hyphens, and underscores."
        ),
    )
    secret = models.CharField(
        max_length=255,
        verbose_name=_('secret'),
        help_text=_(
            "When provided, the request will include an HMAC hex digest of the payload body "
            "computed with this secret as the key. The secret itself is not transmitted in the "
            "request."
        ),
    )
    status = models.CharField(
        max_length=20,
        choices=WebhookSecretStatusChoices,
        default=WebhookSecretStatusChoices.STATUS_ENABLED,
        verbose_name=_('status'),
        help_text=_(
            "Enabled secrets sign outgoing requests. Disabled secrets are retained but do not "
            "sign. Retired secrets are permanently out of service and no longer sign new events."
        ),
    )
    is_primary = models.BooleanField(
        default=False,
        verbose_name=_('primary'),
        help_text=_(
            "The primary secret signs the legacy <code>X-Hook-Signature</code> header. Exactly one "
            "enabled secret must be marked primary."
        ),
    )

    class Meta:
        ordering = ('key_id',)
        constraints = (
            models.UniqueConstraint(
                fields=('webhook', 'key_id'),
                name='extras_webhooksecret_unique_webhook_key_id',
            ),
            models.UniqueConstraint(
                fields=('webhook',),
                condition=models.Q(is_primary=True),
                name='extras_webhooksecret_one_primary_per_webhook',
            ),
        )
        verbose_name = _('webhook signing secret')
        verbose_name_plural = _('webhook signing secrets')

    def __str__(self):
        return f'{self.webhook.name}/{self.key_id}'

    def clean(self):
        super().clean()

        errors = {}
        if not self.key_id:
            errors['key_id'] = _('A key ID is required.')
        elif not WEBHOOK_KEY_ID_RE.match(self.key_id):
            errors['key_id'] = _('Only letters, numbers, hyphens, and underscores are allowed.')
        if not self.secret:
            errors['secret'] = _('A secret value is required.')
        if self.is_primary and self.status != WebhookSecretStatusChoices.STATUS_ENABLED:
            errors['is_primary'] = _('The primary signing secret must be enabled.')
        if errors:
            raise ValidationError(errors)


class CustomLink(CloningMixin, ExportTemplatesMixin, OwnerMixin, ChangeLoggedModel):
    """
    A custom link to an external representation of a NetBox object. The link text and URL fields accept Jinja2 template
    code to be rendered with an object as context.
    """
    object_types = models.ManyToManyField(
        to='contenttypes.ContentType',
        related_name='custom_links',
        help_text=_('The object type(s) to which this link applies.')
    )
    name = models.CharField(
        verbose_name=_('name'),
        max_length=100,
        unique=True
    )
    enabled = models.BooleanField(
        verbose_name=_('enabled'),
        default=True
    )
    link_text = models.TextField(
        verbose_name=_('link text'),
        help_text=_("Jinja2 template code for link text")
    )
    link_url = models.TextField(
        verbose_name=_('link URL'),
        help_text=_("Jinja2 template code for link URL")
    )
    weight = models.PositiveSmallIntegerField(
        verbose_name=_('weight'),
        default=100
    )
    group_name = models.CharField(
        verbose_name=_('group name'),
        max_length=50,
        blank=True,
        help_text=_("Links with the same group will appear as a dropdown menu")
    )
    button_class = models.CharField(
        verbose_name=_('button class'),
        max_length=30,
        choices=CustomLinkButtonClassChoices,
        default=CustomLinkButtonClassChoices.DEFAULT,
        help_text=_("The class of the first link in a group will be used for the dropdown button")
    )
    new_window = models.BooleanField(
        verbose_name=_('new window'),
        default=False,
        help_text=_("Force link to open in a new window")
    )

    clone_fields = (
        'object_types', 'enabled', 'weight', 'group_name', 'button_class', 'new_window',
    )

    class Meta:
        ordering = ['group_name', 'weight', 'name']
        indexes = (
            models.Index(fields=('group_name', 'weight', 'name')),  # Default ordering
        )
        verbose_name = _('custom link')
        verbose_name_plural = _('custom links')

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse('extras:customlink', args=[self.pk])

    @property
    def docs_url(self):
        return f'{settings.STATIC_URL}docs/models/extras/customlink/'

    def render(self, context):
        """
        Render the CustomLink given the provided context, and return the text, link, and link_target.

        :param context: The context passed to Jinja2
        """
        text = render_jinja2(self.link_text, context).strip()
        if not text:
            return {}
        link = render_jinja2(self.link_url, context).strip()
        link_target = ' target="_blank"' if self.new_window else ''

        # Sanitize link text
        allowed_schemes = get_config().ALLOWED_URL_SCHEMES
        text = clean_html(text, allowed_schemes)

        # Sanitize link
        link = urllib.parse.quote(link, safe='/:?&=%+[]@#,;!')

        # Verify link scheme is allowed
        result = urllib.parse.urlparse(link)
        if result.scheme and result.scheme not in allowed_schemes:
            link = ""

        return {
            'text': text,
            'link': link,
            'link_target': link_target,
        }


class ExportTemplate(
    SyncedDataMixin,
    CloningMixin,
    ExportTemplatesMixin,
    OwnerMixin,
    ChangeLoggedModel,
    RenderTemplateMixin,
):
    object_types = models.ManyToManyField(
        to='contenttypes.ContentType',
        related_name='export_templates',
        help_text=_('The object type(s) to which this template applies.')
    )
    name = models.CharField(
        verbose_name=_('name'),
        max_length=100
    )
    description = models.CharField(
        verbose_name=_('description'),
        max_length=200,
        blank=True
    )

    clone_fields = (
        'object_types', 'template_code', 'mime_type', 'file_name', 'file_extension', 'as_attachment',
    )

    class Meta:
        ordering = ('name',)
        indexes = (
            models.Index(fields=('name',)),  # Default ordering
        )
        verbose_name = _('export template')
        verbose_name_plural = _('export templates')

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse('extras:exporttemplate', args=[self.pk])

    @property
    def docs_url(self):
        return f'{settings.STATIC_URL}docs/models/extras/exporttemplate/'

    def clean(self):
        super().clean()

        if self.name.lower() == 'table':
            raise ValidationError({
                'name': _('"{name}" is a reserved name. Please choose a different name.').format(name=self.name)
            })

    def sync_data(self):
        """
        Synchronize template content from the designated DataFile (if any).
        """
        self.template_code = self.data_file.data_as_string
    sync_data.alters_data = True

    def get_context(self, context=None, queryset=None):
        _context = super().get_context(context=context, queryset=queryset)
        _context['queryset'] = queryset
        return _context


class SavedFilter(CloningMixin, ExportTemplatesMixin, OwnerMixin, ChangeLoggedModel):
    """
    A set of predefined keyword parameters that can be reused to filter for specific objects.
    """
    object_types = models.ManyToManyField(
        to='contenttypes.ContentType',
        related_name='saved_filters',
        help_text=_('The object type(s) to which this filter applies.')
    )
    name = models.CharField(
        verbose_name=_('name'),
        max_length=100,
        unique=True
    )
    slug = models.SlugField(
        verbose_name=_('slug'),
        max_length=100,
        unique=True
    )
    description = models.CharField(
        verbose_name=_('description'),
        max_length=200,
        blank=True
    )
    user = models.ForeignKey(
        to=settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True
    )
    weight = models.PositiveSmallIntegerField(
        verbose_name=_('weight'),
        default=100
    )
    enabled = models.BooleanField(
        verbose_name=_('enabled'),
        default=True
    )
    shared = models.BooleanField(
        verbose_name=_('shared'),
        default=True
    )
    parameters = models.JSONField(
        verbose_name=_('parameters')
    )

    objects = SharedObjectQuerySet.as_manager()

    clone_fields = (
        'object_types', 'weight', 'enabled', 'parameters',
    )

    class Meta:
        ordering = ('weight', 'name')
        indexes = (
            models.Index(fields=('weight', 'name')),  # Default ordering
        )
        verbose_name = _('saved filter')
        verbose_name_plural = _('saved filters')

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse('extras:savedfilter', args=[self.pk])

    @property
    def docs_url(self):
        return f'{settings.STATIC_URL}docs/models/extras/savedfilter/'

    def clean(self):
        super().clean()

        # Verify that `parameters` is a JSON object
        if type(self.parameters) is not dict:
            raise ValidationError(
                {'parameters': _('Filter parameters must be stored as a dictionary of keyword arguments.')}
            )

    @property
    def url_params(self):
        qd = dict_to_querydict(self.parameters)
        return qd.urlencode()


class TableConfig(CloningMixin, ChangeLoggedModel):
    """
    A saved configuration of columns and ordering which applies to a specific table.
    """
    object_type = models.ForeignKey(
        to='contenttypes.ContentType',
        on_delete=models.CASCADE,
        related_name='table_configs',
        help_text=_("The table's object type"),
    )
    table = models.CharField(
        verbose_name=_('table'),
        max_length=100,
    )
    name = models.CharField(
        verbose_name=_('name'),
        max_length=100,
    )
    description = models.CharField(
        verbose_name=_('description'),
        max_length=200,
        blank=True,
    )
    user = models.ForeignKey(
        to=settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
    )
    weight = models.PositiveSmallIntegerField(
        verbose_name=_('weight'),
        default=1000,
    )
    enabled = models.BooleanField(
        verbose_name=_('enabled'),
        default=True
    )
    shared = models.BooleanField(
        verbose_name=_('shared'),
        default=True
    )
    columns = ArrayField(
        base_field=models.CharField(max_length=100),
    )
    ordering = ArrayField(
        base_field=models.CharField(max_length=100),
        blank=True,
        null=True,
    )

    objects = SharedObjectQuerySet.as_manager()

    clone_fields = ('object_type', 'table', 'enabled', 'shared', 'columns', 'ordering')

    class Meta:
        ordering = ('weight', 'name')
        indexes = (
            models.Index(fields=('weight', 'name')),  # Default ordering
        )
        verbose_name = _('table config')
        verbose_name_plural = _('table configs')

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse('extras:tableconfig', args=[self.pk])

    @property
    def docs_url(self):
        return f'{settings.STATIC_URL}docs/models/extras/tableconfig/'

    @property
    def table_class(self):
        return get_table_for_model(self.object_type.model_class(), name=self.table)

    @property
    def ordering_items(self):
        """
        Return a list of two-tuples indicating the column(s) by which the table is to be ordered and a boolean for each
        column indicating whether its ordering is ascending.
        """
        items = []
        for col in self.ordering or []:
            if col.startswith('-'):
                ascending = False
                col = col[1:]
            else:
                ascending = True
            items.append((col, ascending))
        return items

    def clean(self):
        super().clean()

        # Skip table validation until the object type and table have been set
        if not self.object_type_id or not self.table:
            return

        # Validate table
        if self.table_class is None:
            raise ValidationError({
                'table': _("Unknown table: {name}").format(name=self.table)
            })

        table = self.table_class([])

        # Validate ordering columns
        for name in self.ordering or []:
            if name.startswith('-'):
                name = name[1:]  # Strip leading hyphen
            if name not in table.columns:
                raise ValidationError({
                    'ordering': _('Unknown column: {name}').format(name=name)
                })

        # Validate selected columns
        for name in self.columns or []:
            if name not in table.columns:
                raise ValidationError({
                    'columns': _('Unknown column: {name}').format(name=name)
                })


class ImageAttachment(ChangeLoggedModel):
    """
    An uploaded image which is associated with an object.
    """
    object_type = models.ForeignKey(
        to='contenttypes.ContentType',
        on_delete=models.CASCADE
    )
    object_id = models.PositiveBigIntegerField()
    parent = GenericForeignKey(
        ct_field='object_type',
        fk_field='object_id'
    )
    image = models.ImageField(
        upload_to=image_upload,
        height_field='image_height',
        width_field='image_width'
    )
    image_height = models.PositiveSmallIntegerField(
        verbose_name=_('image height'),
    )
    image_width = models.PositiveSmallIntegerField(
        verbose_name=_('image width'),
    )
    # Unlike image_height/image_width (populated automatically by ImageField), there is no native size_field, so
    # this is populated in save(). It is nullable because existing rows predate the field and storage reads can
    # fail; a null value means "not yet computed" and the size property falls back to reading storage.
    image_size = models.PositiveBigIntegerField(
        verbose_name=_('image size'),
        blank=True,
        null=True,
    )
    name = models.CharField(
        verbose_name=_('name'),
        max_length=50,
        blank=True
    )
    description = models.CharField(
        verbose_name=_('description'),
        max_length=200,
        blank=True
    )

    objects = RestrictedQuerySet.as_manager()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Cache an identity for the current image so save() can detect a new/replaced file and recompute the cached
        # image_size. We combine the file name with the (auto-populated) dimensions: a replacement that reuses the
        # same name is still caught when its dimensions differ. Read the raw image value from __dict__ to avoid
        # triggering the ImageField descriptor here (doing so during ORM/GraphQL instantiation can recurse).
        self._orig_image_key = self._image_identity()

    def _image_identity(self):
        """
        Return a tuple identifying the current image file for change detection: its name plus the dimensions Django
        populates from it. All three are read raw from __dict__ to avoid triggering the ImageField descriptor
        (accessing `self.image` during ORM/GraphQL instantiation can recurse). Not a content fingerprint: a
        replacement with an identical name AND identical dimensions is not distinguished (would require reading the
        file, the storage round-trip this caching avoids).
        """
        original = self.__dict__.get('image')
        name = getattr(original, 'name', original)
        return (name, self.__dict__.get('image_height'), self.__dict__.get('image_width'))

    class Meta:
        ordering = ('name', 'pk')  # name may be non-unique
        indexes = (
            models.Index(fields=('name', 'id')),  # Default ordering
            models.Index(fields=('object_type', 'object_id')),
        )
        verbose_name = _('image attachment')
        verbose_name_plural = _('image attachments')

    def __str__(self):
        return self.name or self.filename

    def get_absolute_url(self):
        return reverse('extras:imageattachment', args=[self.pk])

    def clean(self):
        super().clean()

        # Validate the assigned object type
        if not has_feature(self.object_type, 'image_attachments'):
            raise ValidationError(
                _("Image attachments cannot be assigned to this object type ({type}).").format(type=self.object_type)
            )

    def delete(self, *args, **kwargs):

        _name = self.image.name

        super().delete(*args, **kwargs)

        # Delete file from disk
        self.image.delete(save=False)

        # Deleting the file erases its name. We restore the image's filename here in case we still need to reference it
        # before the request finishes. (For example, to display a message indicating the ImageAttachment was deleted.)
        self.image.name = _name

    @property
    def filename(self):
        base_name = Path(self.image.name).name
        prefix = f"{self.object_type.model}_{self.object_id}_"
        return base_name.removeprefix(prefix)

    @property
    def html_tag(self):
        """
        Returns a complete <img> tag suitable for embedding in an HTML document.
        """
        return mark_safe('<img src="{url}" height="{height}" width="{width}" alt="{alt_text}" />'.format(
            url=self.image.url,
            height=self.image_height,
            width=self.image_width,
            alt_text=escape(self.description or self.name),
        ))

    def _read_image_size(self):
        """
        Read the image file's size from storage, suppressing an OSError in case the file is inaccessible. Also
        opportunistically catch other exceptions that we know other storage back-ends to throw. Returns None if the
        size cannot be determined. This may issue a request to the storage backend (e.g. a HEAD request to S3).
        """
        if not self.image:
            return None

        expected_exceptions = [OSError]

        try:
            from botocore.exceptions import ClientError
            expected_exceptions.append(ClientError)
        except ImportError:
            pass

        try:
            return self.image.size
        except tuple(expected_exceptions):
            return None

    @property
    def size(self):
        """
        Return the size of the image file in bytes. Prefer the cached `image_size` value to avoid a storage request;
        fall back to reading from storage for legacy rows where `image_size` has not yet been populated.
        """
        if self.image_size is not None:
            return self.image_size
        return self._read_image_size()

    def save(self, *args, **kwargs):
        # Populate image_size on creation or when the image file has changed. Reading the size may touch the storage
        # backend (e.g. a HEAD request to S3), so we only do it when necessary: bulk operations that don't alter the
        # image (bulk edit, rename) leave the identity unchanged and skip the read entirely. We never overwrite a good
        # value with None (e.g. on a transient storage error); a failed read while replacing a file keeps the prior
        # size until the next successful save, which is preferred over storing None.
        orig_image_key = getattr(self, '_orig_image_key', None)
        if self._state.adding or self._image_identity() != orig_image_key:
            size = self._read_image_size()
            if size is not None:
                self.image_size = size

        super().save(*args, **kwargs)

        # Refresh the cached identity so subsequent saves on this instance detect further changes correctly.
        self._orig_image_key = self._image_identity()

    def to_objectchange(self, action):
        objectchange = super().to_objectchange(action)
        objectchange.related_object = self.parent
        return objectchange


class JournalEntry(CustomFieldsMixin, CustomLinksMixin, TagsMixin, ExportTemplatesMixin, ChangeLoggedModel):
    """
    A historical remark concerning an object; collectively, these form an object's journal. The journal is used to
    preserve historical context around an object, and complements NetBox's built-in change logging. For example, you
    might record a new journal entry when a device undergoes maintenance, or when a prefix is expanded.
    """
    assigned_object_type = models.ForeignKey(
        to='contenttypes.ContentType',
        on_delete=models.CASCADE
    )
    assigned_object_id = models.PositiveBigIntegerField()
    assigned_object = GenericForeignKey(
        ct_field='assigned_object_type',
        fk_field='assigned_object_id'
    )
    created_by = models.ForeignKey(
        to=settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True
    )
    kind = models.CharField(
        verbose_name=_('kind'),
        max_length=30,
        choices=JournalEntryKindChoices,
        default=JournalEntryKindChoices.KIND_INFO
    )
    comments = models.TextField(
        verbose_name=_('comments'),
    )

    class Meta:
        ordering = ('-created',)
        indexes = (
            models.Index(fields=('-created',)),  # Default ordering
            models.Index(fields=('assigned_object_type', 'assigned_object_id')),
        )
        verbose_name = _('journal entry')
        verbose_name_plural = _('journal entries')

    def __str__(self):
        created = timezone.localtime(self.created)
        return (
            f"{created.date().isoformat()} {created.time().isoformat(timespec='minutes')} "
            f"({self.get_kind_display()})"
        )

    def get_absolute_url(self):
        return reverse('extras:journalentry', args=[self.pk])

    def clean(self):
        super().clean()

        # Validate the assigned object type
        if not has_feature(self.assigned_object_type, 'journaling'):
            raise ValidationError(
                _("Journaling is not supported for this object type ({type}).").format(type=self.assigned_object_type)
            )

    def get_kind_color(self):
        return JournalEntryKindChoices.colors.get(self.kind)


class Bookmark(models.Model):
    """
    An object bookmarked by a User.
    """
    created = models.DateTimeField(
        verbose_name=_('created'),
        auto_now_add=True
    )
    object_type = models.ForeignKey(
        to='contenttypes.ContentType',
        on_delete=models.PROTECT
    )
    object_id = models.PositiveBigIntegerField()
    object = GenericForeignKey(
        ct_field='object_type',
        fk_field='object_id'
    )
    user = models.ForeignKey(
        to=settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE
    )

    objects = RestrictedQuerySet.as_manager()

    class Meta:
        ordering = ('created', 'pk')
        indexes = (
            models.Index(fields=('created', 'id')),  # Default ordering
            models.Index(fields=('object_type', 'object_id')),
        )
        constraints = (
            models.UniqueConstraint(
                fields=('object_type', 'object_id', 'user'),
                name='%(app_label)s_%(class)s_unique_per_object_and_user'
            ),
        )
        verbose_name = _('bookmark')
        verbose_name_plural = _('bookmarks')

    def __str__(self):
        if self.object:
            return str(self.object)
        return super().__str__()

    def get_absolute_url(self):
        return reverse('account:bookmarks')

    def clean(self):
        super().clean()

        # Validate the assigned object type
        if not has_feature(self.object_type, 'bookmarks'):
            raise ValidationError(
                _("Bookmarks cannot be assigned to this object type ({type}).").format(type=self.object_type)
            )
