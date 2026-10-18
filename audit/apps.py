from django.apps import AppConfig


class AuditConfig(AppConfig):
    name = "audit"
    verbose_name = "Audit log"

    def ready(self):
        from . import checks, signals  # noqa: F401  (registers the system check)
        signals.connect()
