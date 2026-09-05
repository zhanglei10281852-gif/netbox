import hashlib
import hmac
import logging

import requests
from django.conf import settings
from django_rq import job
from jinja2.exceptions import TemplateError

from netbox.registry import registry
from netbox.settings_utils import parse_job_timeout
from utilities.proxy import resolve_proxies
from utilities.request import get_safe_request_context

from .constants import WEBHOOK_EVENT_TYPES

__all__ = (
    'generate_signature',
    'register_webhook_callback',
    'send_webhook',
)

logger = logging.getLogger('netbox.webhooks')

# Header carrying the signature of the primary signing key (backward compatible).
SIGNATURE_HEADER = 'X-Hook-Signature'
# Header carrying one signature per enabled key, keyed by the key's stable identifier,
# as comma-separated "<key_id>=<hexdigest>" entries.
SIGNATURES_HEADER = 'X-Hook-Signatures'


def register_webhook_callback(func):
    """
    Register a function as a webhook callback.
    """
    registry['webhook_callbacks'].append(func)
    logger.debug(f'Registered webhook callback {func.__module__}.{func.__name__}')
    return func


def generate_signature(request_body, secret):
    """
    Return a cryptographic signature that can be used to verify the authenticity of webhook data.
    """
    hmac_prep = hmac.new(
        key=secret.encode('utf8'),
        msg=request_body,
        digestmod=hashlib.sha512
    )
    return hmac_prep.hexdigest()


def sign_request(prepared_request, signing_keys):
    """
    Attach HMAC signatures of the (final) request body to the prepared request headers.

    :param signing_keys: A list of {'key_id', 'secret', 'is_primary'} dicts, as snapshotted at
        enqueue time. Each enabled key produces an entry in the X-Hook-Signatures header; the
        primary key additionally signs the legacy X-Hook-Signature header. When empty, no
        signature headers are added (the webhook has no signing secrets configured).
    """
    if not signing_keys:
        return

    signatures = {
        key['key_id']: generate_signature(prepared_request.body, key['secret'])
        for key in signing_keys
    }
    primary_key = next((key for key in signing_keys if key.get('is_primary')), None)
    if primary_key is not None:
        prepared_request.headers[SIGNATURE_HEADER] = signatures[primary_key['key_id']]
    prepared_request.headers[SIGNATURES_HEADER] = ','.join(
        f'{key_id}={signature}' for key_id, signature in signatures.items()
    )


@job('default')
def send_webhook(event_rule, object_type, event_type, data, timestamp, request=None, snapshots=None,
                signing_keys=None):
    """
    Make a POST request to the defined Webhook
    """
    webhook = event_rule.action_object

    # Prepare context data for headers & body templates
    context = {
        'event': WEBHOOK_EVENT_TYPES.get(event_type, event_type),
        'timestamp': timestamp,
        'object_type': '.'.join(object_type.natural_key()),
        'data': data,
    }
    if request:
        context['request'] = get_safe_request_context(request)
    if snapshots:
        context['snapshots'] = snapshots

    # Add any additional context from plugins
    callback_data = {}
    for callback in registry['webhook_callbacks']:
        try:
            if ret := callback(object_type, event_type, data, request):
                callback_data.update(**ret)
        except Exception as e:
            logger.warning(f"Caught exception when processing callback {callback}: {e}")
            pass
    if callback_data:
        context['context'] = callback_data

    # Build the headers for the HTTP request
    headers = {
        'Content-Type': webhook.http_content_type,
    }
    try:
        headers.update(webhook.render_headers(context))
    except (TemplateError, ValueError) as e:
        logger.error(f"Error parsing HTTP headers for webhook {webhook}: {e}")
        raise e

    # Render the request body
    try:
        body = webhook.render_body(context)
    except TemplateError as e:
        logger.error(f"Error rendering request body for webhook {webhook}: {e}")
        raise e

    # Prepare the HTTP request
    url = webhook.render_payload_url(context)
    params = {
        'method': webhook.http_method,
        'url': url,
        'headers': headers,
        'data': body.encode('utf8'),
    }
    logger.info(
        f"Sending {params['method']} request to {params['url']} ({context['object_type']} {context['event']})"
    )
    logger.debug(params)
    try:
        prepared_request = requests.Request(**params).prepare()
    except requests.exceptions.RequestException as e:
        logger.error(f"Error forming HTTP request: {e}")
        raise e

    # Sign the request using the signing keys snapshotted when the event was enqueued. Jobs
    # enqueued before multi-secret support (which carry no snapshot) fall back to the webhook's
    # current signing configuration, matching the legacy behavior of reading the secret at send
    # time.
    if signing_keys is None:
        signing_keys = webhook.get_signing_keys_snapshot()
    sign_request(prepared_request, signing_keys)

    # Determine the request timeout, preferring the webhook-specific value over the global default
    timeout = webhook.timeout if webhook.timeout is not None else settings.WEBHOOK_DEFAULT_TIMEOUT

    # Webhook.clean() enforces this when the webhook is saved, but RQ_DEFAULT_TIMEOUT may have been lowered since.
    job_timeout = parse_job_timeout(settings.RQ_DEFAULT_TIMEOUT)
    if job_timeout is not None and timeout >= job_timeout:
        logger.warning(
            f"Webhook timeout ({timeout} seconds) is not less than the background job timeout ({job_timeout} "
            f"seconds); the job may be terminated before the request can time out."
        )

    # Send the request
    with requests.Session() as session:
        session.verify = webhook.ssl_verification
        if webhook.ca_file_path:
            session.verify = webhook.ca_file_path
        proxies = resolve_proxies(url=url, context={'client': webhook})
        try:
            response = session.send(prepared_request, proxies=proxies, timeout=timeout)
        except requests.exceptions.Timeout:
            logger.error(f"Request to {url} timed out after {timeout} seconds")
            raise

    if 200 <= response.status_code <= 299:
        logger.info(f"Request succeeded; response status {response.status_code}")
        return f"Status {response.status_code} returned, webhook successfully processed."
    logger.warning(f"Request failed; response status {response.status_code}: {response.content}")
    raise requests.exceptions.RequestException(
        f"Status {response.status_code} returned with content '{response.content}', webhook FAILED to process."
    )
