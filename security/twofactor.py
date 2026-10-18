"""Setting up, checking and removing two-factor sign-in."""
import hashlib
import hmac
import logging
import secrets

from django.apps import apps
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import totp
from .models import RecoveryCode, TwoFactor

logger = logging.getLogger("security")

CODE_COUNT = 8
ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"                           # no 0/o, 1/l/i: easy to copy from paper


def policy_requires(user):
    """True when the settings require this user to have two-factor sign-in (administrators, if so configured)."""
    return bool(getattr(settings, "TWO_FACTOR_REQUIRED_FOR_ADMINS", False) and user.is_superuser)


def confirmed(user):
    return TwoFactor.objects.filter(user=user, confirmed=True).first()


def is_enabled(user):
    return confirmed(user) is not None


# ---------------------------------------------------------------- recovery codes
def _hash(code):
    """High-entropy random codes do not need a slow hash; the site's secret key keeps the hashes unguessable."""
    normal = code.lower().replace("-", "").replace(" ", "")
    return hmac.new(settings.SECRET_KEY.encode(), b"recovery:" + normal.encode(), hashlib.sha256).hexdigest()


def _new_codes(user):
    """Replace all recovery codes. Returns the plain codes: this is the only time they exist."""
    RecoveryCode.objects.filter(user=user).delete()
    codes = ["".join(secrets.choice(ALPHABET) for _ in range(10)) for _ in range(CODE_COUNT)]
    RecoveryCode.objects.bulk_create([RecoveryCode(user=user, code_hash=_hash(c)) for c in codes])
    return [f"{c[:5]}-{c[5:]}" for c in codes]


def codes_left(user):
    return RecoveryCode.objects.filter(user=user, used_at__isnull=True).count()


# ---------------------------------------------------------------- setting up
def pending(user):
    """The pending secret for someone setting up, kept the same while they scan it. None if already switched on."""
    existing = TwoFactor.objects.filter(user=user).first()
    if existing and existing.confirmed:
        return None
    if existing and not existing.secret:                  # unreadable (the encryption key changed): start again
        existing.secret = totp.new_secret()
        existing.save(update_fields=["secret"])
    return existing or TwoFactor.objects.create(user=user, secret=totp.new_secret())


def uri(user_secret, username):
    return totp.provisioning_uri(user_secret, username, getattr(settings, "TWO_FACTOR_ISSUER", "Payroll"))


def _use_step(tf, step):
    """Claim a time step with one atomic update, so the same code cannot be accepted twice, even at the same instant."""
    return TwoFactor.objects.filter(pk=tf.pk, last_step__lt=step).update(last_step=step) == 1


@transaction.atomic
def confirm(user, code):
    """Switch two-factor on if the code from the user's app is right. Returns the recovery codes, or None."""
    tf = TwoFactor.objects.select_for_update().filter(user=user, confirmed=False).first()
    step = totp.verify(tf.secret, code, tf.last_step) if tf else None
    if step is None or not _use_step(tf, step):
        return None
    tf.refresh_from_db()
    tf.confirmed, tf.confirmed_at = True, timezone.now()
    tf.save(update_fields=["confirmed", "confirmed_at"])          # a normal save, so the audit log records who switched it on
    return _new_codes(user)


# ---------------------------------------------------------------- checking a code at sign-in
def check_code(user, code):
    """'totp', 'recovery' or None. An authenticator code works once; a recovery code works once, ever."""
    tf = confirmed(user)
    if tf is None:
        return None
    if not tf.secret:
        logger.error("The two-factor secret for %s cannot be read (was PAYROLL_ENCRYPTION_KEY changed?). Codes from the "
                     "app are refused until an administrator resets it; recovery codes still work.", user.get_username())
    step = totp.verify(tf.secret, code, tf.last_step)
    if step is not None and _use_step(tf, step):
        return "totp"
    wanted = _hash(code or "")
    for row in RecoveryCode.objects.filter(user=user, used_at__isnull=True):
        if hmac.compare_digest(row.code_hash, wanted):
            if RecoveryCode.objects.filter(pk=row.pk, used_at__isnull=True).update(used_at=timezone.now()) == 1:
                return "recovery"
    return None


def regenerate_codes(user):
    if not is_enabled(user):
        raise ValueError("Two-factor sign-in is not switched on.")
    return _new_codes(user)


def disable(user):
    TwoFactor.objects.filter(user=user).delete()
    RecoveryCode.objects.filter(user=user).delete()


def audit(summary, detail=""):
    """Note something in the audit log, when that module is installed."""
    if apps.is_installed("audit"):
        from audit.recorder import record_event
        record_event(summary, detail)
