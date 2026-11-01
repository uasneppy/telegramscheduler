"""Typed errors for the web board; the server maps them to JSON responses."""


class WebError(Exception):
    """An error with an HTTP status, a stable machine-readable code and optional extra body fields."""

    def __init__(self, status: int, code: str, message: str, extra: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        # A dict, not **kwargs, so extra keys such as "status" cannot collide with the arguments.
        self.extra = dict(extra or {})


def invalid_request(message: str) -> WebError:
    return WebError(400, "invalid_request", message)


def not_found(message: str = "Not found") -> WebError:
    return WebError(404, "not_found", message)


def conflict(message: str, **extra) -> WebError:
    return WebError(409, "conflict", message, extra)


def too_large(message: str) -> WebError:
    return WebError(413, "too_large", message)
