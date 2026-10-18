from django.conf import settings
from django.db import models
from django.utils import timezone

from payroll.fields import EncryptedCharField


class LoginFailure(models.Model):
    """A wrong password or two-factor code. Only the name that was tried and where it came from are kept."""
    username = models.CharField(max_length=150, db_index=True)         # lower case; "2fa:" is put in front for codes
    ip_address = models.GenericIPAddressField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)


class TwoFactor(models.Model):
    """A user's authenticator-app setup. Until confirmed it is only a pending secret and protects nothing."""
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="two_factor")
    secret = EncryptedCharField()                                       # encrypted at rest, like the mail password
    confirmed = models.BooleanField(default=False)
    last_step = models.BigIntegerField(default=0)                       # the newest time step used: a code works once
    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.user.get_username()} ({'on' if self.confirmed else 'pending'})"


class RecoveryCode(models.Model):
    """One of the single-use codes that let someone in after losing their phone. Only a hash is kept."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="recovery_codes")
    code_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    used_at = models.DateTimeField(null=True, blank=True)
