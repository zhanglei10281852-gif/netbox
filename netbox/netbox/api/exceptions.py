from rest_framework import status
from rest_framework.exceptions import APIException


class ServiceUnavailable(APIException):
    status_code = 503
    default_detail = "Service temporarily unavailable, please try again later."


class IdempotencyKeyConflict(APIException):
    """
    Raised when an Idempotency-Key is reused within the same scope (authenticated user, HTTP
    method, normalized path) but with a different request body or write-significant query
    parameters than the original request.
    """
    status_code = status.HTTP_409_CONFLICT
    default_detail = (
        'This Idempotency-Key has already been used with a different request payload. '
        'Reuse a key only when retrying the identical request.'
    )
    default_code = 'idempotency_key_conflict'


class IdempotencyKeyInProgress(APIException):
    """
    Raised when a request carrying an Idempotency-Key arrives while the first request with that
    key is still executing and the bounded lock wait elapses before it completes. The client may
    retry shortly and will receive the stored result.
    """
    status_code = status.HTTP_409_CONFLICT
    default_detail = (
        'A request with this Idempotency-Key is still being processed. Retry the request '
        'shortly to receive the original result.'
    )
    default_code = 'idempotency_key_in_progress'


class SerializerNotFound(Exception):
    pass


class GraphQLTypeNotFound(Exception):
    pass


class QuerySetNotOrdered(Exception):
    pass
