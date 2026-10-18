from django.conf import settings
from django.core.checks import Error, register


@register()
def middleware_is_installed(app_configs, **kwargs):
    """Without the middleware every entry would be written with no name on it, so refuse to run that way."""
    if "audit.middleware.AuditMiddleware" in settings.MIDDLEWARE:
        return []
    return [Error("The audit app is installed but its middleware is not, so entries would not say who acted.",
                  hint="Add 'audit.middleware.AuditMiddleware' to MIDDLEWARE in config/settings.py.",
                  id="audit.E001")]
