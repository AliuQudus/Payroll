from django.conf import settings
from django.core.checks import Error, register


@register()
def two_factor_middleware_is_installed(app_configs, **kwargs):
    """Without the gate, people would switch two-factor on and it would silently protect nothing."""
    if "security.middleware.TwoFactorMiddleware" in settings.MIDDLEWARE:
        return []
    return [Error("The security app is installed but its two-factor middleware is not, so two-factor sign-in would "
                  "not be enforced.",
                  hint="Add 'security.middleware.TwoFactorMiddleware' to MIDDLEWARE in config/settings.py, after "
                       "AuthenticationMiddleware.", id="security.E010")]
