from . import current
from .recorder import record_event
from .registry import WATCHED


class AuditMiddleware:
    """Tells the audit log who is acting, and records downloads and other actions that change no data."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = current.set_request(request)
        try:
            response = self.get_response(request)
            self.watch(request, response)
            return response
        finally:
            current.reset_request(token)

    def watch(self, request, response):
        match = request.resolver_match
        spec = WATCHED.get(match.view_name) if match else None
        if not spec or request.method != spec[0] or response.status_code not in (200, 302):
            return
        if not request.user.is_authenticated:
            return
        detail = ", ".join(f"{k}={v}" for k, v in match.kwargs.items())
        record_event(spec[1], detail, company=getattr(request, "company", None))
