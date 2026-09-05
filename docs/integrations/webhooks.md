# Webhooks

NetBox can be configured via [Event Rules](../features/event-rules.md) to transmit outgoing webhooks to remote systems in response to internal object changes. The receiver can act on the data in these webhook messages to perform related tasks.

For example, suppose you want to automatically configure a monitoring system to start monitoring a device when its operational status is changed to active, and remove it from monitoring for any other status. You can create a webhook in NetBox for the device model and craft its content and destination URL to effect the desired change on the receiving system. Webhooks will be sent automatically by NetBox whenever the configured constraints are met.

!!! warning "Security Notice"
    Webhooks support the inclusion of user-submitted code to generate the URL, custom headers, and payloads, which may pose security risks under certain conditions. Only grant permission to create or modify webhooks to trusted users.

## Jinja2 Template Support

[Jinja2 templating](https://jinja.palletsprojects.com/) is supported for the `URL`, `additional_headers` and `body_template` fields. This enables the user to convey object data in the request headers as well as to craft a customized request body. Request content can be crafted to enable the direct interaction with external systems by ensuring the outgoing message is in a format the receiver expects and understands.

For example, you might create a NetBox webhook to [trigger a Slack message](https://api.slack.com/messaging/webhooks) any time an IP address is created. You can accomplish this using the following configuration:

* Object type: IPAM > IP address
* HTTP method: `POST`
* URL: Slack incoming webhook URL
* HTTP content type: `application/json`
* Body template: `{"text": "IP address {{ data['address'] }} was created by {{ request.user }}!"}`

### Available Context

The following data is available as context for Jinja2 templates:

* `event` - The type of event which triggered the webhook: `created`, `updated`, or `deleted`.
* `timestamp` - The time at which the event occurred (in [ISO 8601](https://en.wikipedia.org/wiki/ISO_8601) format).
* `object_type` - The NetBox model which triggered the change in the form `app_label.model_name`.
* `request` - Data about the triggering request (if available).
    * `request.id` - The UUID associated with the request
    * `request.method` - The HTTP method (e.g. `GET` or `POST`)
    * `request.path` - The URL path (ex: `/dcim/sites/123/edit/`)
    * `request.path_info` - The URL path below the application script prefix
    * `request.GET` - The query parameters included in the request
    * `request.user` - The name of the authenticated user who made the request (if available)
* `data` - A detailed representation of the object in its current state. This is typically equivalent to the model's representation in NetBox's REST API.
* `snapshots` - Minimal "snapshots" of the object state both before and after the change was made; provided as a dictionary with keys named `prechange` and `postchange`. These are not as extensive as the fully serialized representation, but contain enough information to convey what has changed.

### Sanitizing Header Values

When rendering the `additional_headers` field, a `header_safe` filter is made available for sanitizing a value for safe inclusion in a raw HTTP header. It strips newlines and other control characters from the rendered value, preventing HTTP header (CR/LF) injection.

Whenever a header value incorporates data which may be influenced by other users (such as an object's attributes), pass it through this filter to avoid smuggling of additional headers. For example:

```
X-Object-Name: {{ data.name | header_safe }}
```

### Default Request Body

If no body template is specified, the request body will be populated with a JSON object containing the context data. For example, a newly created site might appear as follows:

```json
{
    "event": "created",
    "timestamp": "2026-03-06T15:11:23.503186+00:00",
    "object_type": "dcim.site",
    "data": {
        "id": 4,
        "url": "/api/dcim/sites/4/",
        "display_url": "/dcim/sites/4/",
        "display": "Site 1",
        "name": "Site 1",
        "slug": "site-1",
        "status": {
            "value": "active",
            "label": "Active"
        },
        "region": null,
        ...
    },
    "request": {
        "id": "17af32f0-852a-46ca-a7d4-33ecd0c13de6",
        "method": "POST",
        "path": "/dcim/sites/add/",
        "user": "jstretch"
    },
    "snapshots": {
        "prechange": null,
        "postchange": {
            "created": "2026-03-06T15:11:23.484Z",
            "owner": null,
            "description": "",
            "comments": "",
            "name": "Site 1",
            "slug": "site-1",
            "status": "active",
            ...
        }
    }
}
```

!!! note
    The setting of conditional webhooks has been moved to [Event Rules](../features/event-rules.md) since NetBox 3.7

## Webhook Processing

Using [Event Rules](../features/event-rules.md), when a change is detected, any resulting webhooks are placed into a Redis queue for processing. This allows the user's request to complete without needing to wait for the outgoing webhook(s) to be processed. The webhooks are then extracted from the queue by the `rqworker` process and HTTP requests are sent to their respective destinations. The current webhook queue and any failed webhooks can be inspected under System > Background Tasks.

A request is considered successful if the response has a 2XX status code; otherwise, the request is marked as having failed. Failed requests may be requeued manually under System > Background Tasks.

## Request Signing & Key Rotation

When a webhook has at least one enabled signing secret, each request carries two headers, both computed as an HMAC (SHA-512) hex digest of the *final* request body using the secret as the key:

* `X-Hook-Signature` — the signature produced by the webhook's **primary** secret. Receivers written for earlier NetBox releases verify this header exactly as before.
* `X-Hook-Signatures` — a comma-separated list of `<key_id>=<signature>` entries, one entry per enabled secret, e.g.:

```
X-Hook-Signatures: primary=9f86d081...,rotated-2026=e3b0c442...
```

The receiver parses the entries, looks up the matching key by `key_id`, and compares the expected HMAC (e.g. with `hmac.compare_digest`) against the signature. Accepting any valid entry allows overlapping keys to coexist during rotation.

Key IDs are stable identifiers containing only letters, numbers, hyphens, and underscores, and are managed on the webhook (in the UI editor or the REST API `secrets` field). Secrets may be **enabled** (sign new events), **disabled** (retained without signing; can be re-enabled), or **retired** (terminal; never signs new events and cannot be modified, only deleted). Exactly one enabled secret must be marked primary.

To rotate the signing key without rejecting valid events:

1. Add the new secret as *enabled* (not yet primary).
2. Deploy the new key to the receiver and configure it to accept signatures from `X-Hook-Signatures` for either key ID.
3. Mark the new secret *primary*. `X-Hook-Signature` then reflects the new key, while `X-Hook-Signatures` still carries both during the overlap.
4. Once the receiver no longer needs the old key, **retire** (or delete) it.

The set of keys used for an event is snapshotted when the event is enqueued and travels with the background job, so changing secrets while events are queued — or an automatic job retry — never changes which signatures a given event carries. Events enqueued after a key is retired or disabled simply do not include that key's signature. Webhooks without any secrets are sent without signature headers.

## Troubleshooting

To assist with verifying that the content of outgoing webhooks is rendered correctly, NetBox provides a simple HTTP listener that can be run locally to receive and display webhook requests. First, modify the target URL of the desired webhook to `http://localhost:9000/`. This will instruct NetBox to send the request to the local server on TCP port 9000. Then, start the webhook receiver service from the NetBox root directory:

```no-highlight
$ python netbox/manage.py webhook_receiver
Listening on port http://localhost:9000. Stop with CONTROL-C.
```

You can test the receiver itself by sending any HTTP request to it. For example:

```no-highlight
$ curl -X POST http://localhost:9000 --data '{"foo": "bar"}'
```

The server will print output similar to the following:

```no-highlight
[1] Tue, 07 Apr 2020 17:44:02 GMT 127.0.0.1 "POST / HTTP/1.1" 200 -
Host: localhost:9000
User-Agent: curl/7.58.0
Accept: */*
Content-Length: 14
Content-Type: application/x-www-form-urlencoded

{"foo": "bar"}
------------
```

Note that `webhook_receiver` does not actually _do_ anything with the information received: It merely prints the request headers and body for inspection. If you don't see any output, check that the `rqworker` process is running and that webhook events are being placed into the queue.

Webhook results can be found in the NetBox admin UI under the Background Tasks section. You can see any finished or failed runs, as well as the error log for failed webhooks.
