"""Remembers the request being handled, so a model save can be traced back to the person who made it."""
from contextvars import ContextVar

_request = ContextVar("audit_request", default=None)


def set_request(request):
    return _request.set(request)


def reset_request(token):
    _request.reset(token)


def actor():
    """(user or None, ip or None) for the current request. Outside a request (a command, a test) both are None."""
    request = _request.get()
    if request is None:
        return None, None
    user = getattr(request, "user", None)
    user = user if user is not None and user.is_authenticated else None
    return user, ip_of(request)


def ip_of(request):
    """The visitor's address, using the security app's proxy-aware rule when it is installed."""
    from django.apps import apps
    if request is None:
        return None
    if apps.is_installed("security"):
        from security.clientip import client_ip
        return client_ip(request)
    return request.META.get("REMOTE_ADDR") or None
