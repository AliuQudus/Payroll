from django.contrib.auth.forms import AuthenticationForm
from django.core.exceptions import ValidationError

from . import lockout
from .clientip import client_ip


class LockoutAuthenticationForm(AuthenticationForm):
    """The sign-in form, saying plainly when a username is locked instead of the usual wrong-password message."""

    def clean(self):
        username = self.cleaned_data.get("username")
        remaining = lockout.lock_remaining(username, client_ip(self.request))
        if remaining is not None:
            raise ValidationError(f"Too many failed sign-in attempts. Try again in {lockout.minutes_text(remaining)}.",
                                  code="locked")
        return super().clean()
