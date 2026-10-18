"""The Django admin site has its own sign-in form, so give it the same friendly lockout message."""
from django.contrib import admin
from django.contrib.admin.forms import AdminAuthenticationForm
from django.core.exceptions import ValidationError

from . import lockout
from .clientip import client_ip


class LockoutAdminAuthenticationForm(AdminAuthenticationForm):
    def clean(self):
        remaining = lockout.lock_remaining(self.cleaned_data.get("username"), client_ip(self.request))
        if remaining is not None:
            raise ValidationError(f"Too many failed sign-in attempts. Try again in {lockout.minutes_text(remaining)}.",
                                  code="locked")
        return super().clean()


def install():
    admin.site.login_form = LockoutAdminAuthenticationForm
