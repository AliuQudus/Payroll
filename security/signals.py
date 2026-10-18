from django.contrib.auth.signals import user_logged_in, user_login_failed
from django.dispatch import receiver

from . import lockout
from .clientip import client_ip


@receiver(user_login_failed)
def note_failure(sender, credentials, request=None, **kwargs):
    name = credentials.get("username", "") or ""
    ip = client_ip(request)
    if lockout.lock_remaining(name, ip) is not None:
        return                      # already locked: more guesses must not extend the lock indefinitely
    lockout.record_failure(name, ip)


@receiver(user_logged_in)
def forget_failures(sender, request, user, **kwargs):
    lockout.clear(user.get_username())
    if request is not None:
        request.session.pop("2fa_ok", None)        # every fresh password sign-in has to pass the second step again
