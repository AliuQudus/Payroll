from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.core.exceptions import PermissionDenied

from . import lockout
from .clientip import client_ip


class LockoutBackend(ModelBackend):
    """The normal sign-in, except while a username or address is locked. It sits in the one place every sign-in
    passes through, so the admin site and any other login form are covered as well as the main one."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        name = username if username is not None else kwargs.get(get_user_model().USERNAME_FIELD)
        if lockout.lock_remaining(name, client_ip(request)) is not None:
            raise PermissionDenied("Too many failed sign-in attempts.")       # even the right password is refused
        return super().authenticate(request, username=username, password=password, **kwargs)
