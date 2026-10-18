"""Turns model changes and events into AuditEntry rows."""
import datetime
import logging
from decimal import Decimal

from django.db.models import DecimalField

from payroll.models import Company, Employee

from . import current
from .models import AuditEntry
from .registry import EXCLUDED, HIDDEN, MASK_WORDS, MASKED

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ which fields, and how to show them
def tracked_fields(model, update_fields=None):
    label = model._meta.label_lower
    skip = EXCLUDED.get(label, set())
    for f in model._meta.concrete_fields:
        if f.primary_key or f.name in skip or getattr(f, "auto_now", False) or getattr(f, "auto_now_add", False):
            continue
        if update_fields is not None and f.name not in update_fields and f.attname not in update_fields:
            continue                       # only what was really saved: other in-memory edits are not changes
        yield f


def is_masked(model, f):
    return f.name in MASKED.get(model._meta.label_lower, ()) or any(w in f.name for w in MASK_WORDS)


def raw(f, obj):
    value = f.value_from_object(obj)
    try:
        value = f.to_python(value)         # "500000" and Decimal("500000.00") are the same salary
    except Exception:
        return value
    if isinstance(f, DecimalField) and value is not None:
        value = value.quantize(Decimal(1).scaleb(-f.decimal_places))      # shown as the database stores it
    return value


def show(model, f, obj, value):
    if value is None:
        return None
    if is_masked(model, f):
        return HIDDEN
    if f.remote_field:
        try:
            return str(getattr(obj, f.name))
        except Exception:
            return f"#{value}"
    if f.choices:
        return str(dict(f.flatchoices).get(value, value))
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    return str(value)


def diff(model, old, new, update_fields=None):
    changes = {}
    for f in tracked_fields(model, update_fields):
        before, after = raw(f, old), raw(f, new)
        if before != after:
            changes[str(f.verbose_name)] = [show(model, f, old, before), show(model, f, new, after)]
    return changes


def snapshot(model, obj, side):
    """Every filled-in field of a new or deleted record: [None, value] when created, [value, None] when deleted."""
    changes = {}
    for f in tracked_fields(model):
        value = raw(f, obj)
        if value in (None, ""):
            continue
        shown = show(model, f, obj, value)
        changes[str(f.verbose_name)] = [None, shown] if side == "new" else [shown, None]
    return changes


# ------------------------------------------------------------------ who and where
def _employee_of(obj):
    if isinstance(obj, Employee):
        return obj
    if getattr(obj, "employee_id", None):
        try:
            return obj.employee
        except Exception:
            return None
    return None


def _company_id(obj, employee):
    if isinstance(obj, Company):
        return obj.pk
    cid = getattr(obj, "company_id", None)
    return cid or (employee.company_id if employee else None)


def _repr(obj):
    try:
        text = str(obj)
    except Exception:
        text = ""
    if not text or text.endswith(f"object ({obj.pk})"):       # Django's unhelpful default name
        links = []
        for f in obj._meta.concrete_fields:
            if f.remote_field:
                try:
                    links.append(str(getattr(obj, f.name)))
                except Exception:
                    pass
        text = ", ".join(links) or f"{obj._meta.verbose_name} #{obj.pk}"
    return text[:200]


# ------------------------------------------------------------------ writing
def _write(user=None, username=None, **fields):
    """Create an entry. The user defaults to whoever is making the current request."""
    actor, ip = current.actor()
    user = user or actor
    if username is None:
        username = user.get_username() if user else ""
    ip_address = fields.pop("ip_address", None) or ip
    return AuditEntry.objects.create(user=user, username=username[:150], ip_address=ip_address, **fields)


def record_model(action, obj, changes):
    if not changes:
        return
    employee = _employee_of(obj)
    _write(action=action, model_label=obj._meta.label_lower, model_name=str(obj._meta.verbose_name).title(),
           object_id=str(obj.pk), object_repr=_repr(obj), employee_id=employee.pk if employee else None,
           subject=str(employee)[:200] if employee else "", company_id=_company_id(obj, employee),
           changes=changes)


def record_event(summary, detail="", company=None):
    _write(action=AuditEntry.Action.EVENT, object_repr=summary[:200], company_id=company.pk if company else None,
           changes={"Detail": [None, detail]} if detail else {})


def record_login(user, ip=None):
    _write(user=user, action=AuditEntry.Action.LOGIN, object_repr="Signed in", ip_address=ip)


def record_failed_login(username, ip=None):
    _write(username=username or "", action=AuditEntry.Action.LOGIN_FAILED, object_repr="Failed sign-in",
           ip_address=ip)
