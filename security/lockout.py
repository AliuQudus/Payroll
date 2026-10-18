"""Slowing down password guessing: too many wrong passwords lock a username (and an address) for a while."""
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from .models import LoginFailure


def _window():
    return timedelta(minutes=settings.LOGIN_LOCKOUT_MINUTES)


def _remaining(failures, limit):
    """Time until fewer than `limit` of these failures are still inside the window, or None when not locked."""
    times = sorted(failures)
    if len(times) < limit:
        return None
    return max(times[len(times) - limit] + _window() - timezone.now(), timedelta(0))


def lock_remaining(username, ip):
    """A timedelta while the username or address is locked, else None."""
    since = timezone.now() - _window()
    recent = LoginFailure.objects.filter(created_at__gte=since)
    left = []
    if username:
        left.append(_remaining(recent.filter(username=username.strip().lower()).values_list("created_at", flat=True),
                               settings.LOGIN_MAX_FAILURES))
    if ip:
        left.append(_remaining(recent.filter(ip_address=ip).values_list("created_at", flat=True),
                               settings.LOGIN_MAX_FAILURES_PER_IP))
    left = [x for x in left if x is not None]
    return max(left) if left else None


def minutes_text(remaining):
    minutes = max(int(remaining.total_seconds() // 60) + 1, 1)
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


def record_failure(username, ip):
    LoginFailure.objects.create(username=(username or "").strip().lower()[:150], ip_address=ip)
    LoginFailure.objects.filter(created_at__lt=timezone.now() - timedelta(days=1)).delete()      # keep the table small


def clear(username):
    """A correct sign-in wipes the username's failures, so a forgetful user is not punished for long."""
    LoginFailure.objects.filter(username=(username or "").strip().lower()).delete()


def code_key(user):
    """Wrong two-factor codes are counted separately from wrong passwords, under their own name."""
    return f"2fa:{user.get_username()}"
