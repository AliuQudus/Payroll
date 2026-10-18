import logging

from django.apps import apps
from django.contrib.auth.signals import user_logged_in, user_login_failed
from django.db.models.signals import post_delete, post_save, pre_save

from . import current, recorder
from .models import AuditEntry
from .registry import AUDITED

logger = logging.getLogger(__name__)


def safely(handler):
    """An audit fault must never stop payroll work, so it is logged and the save carries on."""
    def wrapper(*args, **kwargs):
        try:
            handler(*args, **kwargs)
        except Exception:
            logger.exception("Audit log could not record a change")
    wrapper.__name__ = handler.__name__
    return wrapper


@safely
def remember_old(sender, instance, raw=False, update_fields=None, **kwargs):
    instance._audit_old = None
    if raw or instance.pk is None:
        return
    instance._audit_old = sender._base_manager.filter(pk=instance.pk).first()


@safely
def after_save(sender, instance, created, raw=False, update_fields=None, **kwargs):
    if raw:
        return
    old = getattr(instance, "_audit_old", None)
    if created or old is None:
        recorder.record_model(AuditEntry.Action.CREATE, instance, recorder.snapshot(sender, instance, "new"))
    else:
        fields = None if update_fields is None else set(update_fields)
        recorder.record_model(AuditEntry.Action.UPDATE, instance, recorder.diff(sender, old, instance, fields))
    instance._audit_old = None


@safely
def after_delete(sender, instance, **kwargs):
    recorder.record_model(AuditEntry.Action.DELETE, instance, recorder.snapshot(sender, instance, "old"))


@safely
def on_login(sender, request, user, **kwargs):
    recorder.record_login(user, current.ip_of(request))


@safely
def on_failed_login(sender, credentials, request=None, **kwargs):
    recorder.record_failed_login(credentials.get("username", ""), current.ip_of(request))


def connect():
    for label in AUDITED:
        try:
            model = apps.get_model(label)          # raises LookupError when that app is not installed
        except LookupError:
            continue
        pre_save.connect(remember_old, sender=model, dispatch_uid=f"audit-pre-{label}")
        post_save.connect(after_save, sender=model, dispatch_uid=f"audit-post-{label}")
        post_delete.connect(after_delete, sender=model, dispatch_uid=f"audit-del-{label}")
    user_logged_in.connect(on_login, dispatch_uid="audit-login")
    user_login_failed.connect(on_failed_login, dispatch_uid="audit-login-failed")
