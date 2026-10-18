from django.contrib.auth import SESSION_KEY
from django.contrib.sessions.models import Session
from django.utils import timezone


def end_sessions(user, keep_key=None):
    """Sign a user out everywhere except (optionally) one session. Returns how many were ended.

    Used when two-factor is switched on or reset, so a session opened before the change, perhaps by someone who had
    only the password, cannot carry on past it.
    """
    ended = 0
    for session in Session.objects.filter(expire_date__gt=timezone.now()).exclude(session_key=keep_key):
        if str(session.get_decoded().get(SESSION_KEY)) == str(user.pk):
            session.delete()
            ended += 1
    return ended
