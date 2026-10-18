from django.apps import AppConfig


class SecurityConfig(AppConfig):
    name = "security"
    verbose_name = "Sign-in protection, backups and health"

    def ready(self):
        from . import checks, signals  # noqa: F401
        from . import admin_login
        admin_login.install()
