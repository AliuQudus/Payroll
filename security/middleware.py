from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.http import urlencode

from . import twofactor

SESSION_KEY = "2fa_ok"          # holds the id of the user who passed the second step in this session


class TwoFactorMiddleware:
    """Holds a signed-in person at the code screen until they have proved the second factor.

    Signing in with a password, by any route (the main page, the admin site, the portal), only gets as far as that
    screen. Nothing else is reachable until the code is accepted, so there is no second way in that skips it.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = request.user
        if user.is_authenticated and request.session.get(SESSION_KEY) != user.pk and not self.exempt(request):
            if twofactor.is_enabled(user):
                return redirect(f"{reverse('security:verify')}?{urlencode({'next': request.get_full_path()})}")
            if twofactor.policy_requires(user):
                messages.warning(request, "Administrators must switch on two-factor sign-in before using the system.")
                return redirect("security:setup")
            request.session[SESSION_KEY] = user.pk        # nothing to prove: remember it, so later requests skip the lookup
        return self.get_response(request)

    @staticmethod
    def exempt(request):
        path = request.path
        return path in (reverse("security:verify"), reverse("security:setup"), reverse("logout"),
                        reverse("security:health")) or path.startswith("/static/")
