from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.conf import settings
from django.db import connection
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from payroll.views import admin_only, layout_template

from . import lockout, qr, twofactor
from .clientip import client_ip
from .forms import LockoutAuthenticationForm
from .middleware import SESSION_KEY
from .sessions import end_sessions


class SecureLoginView(LoginView):
    authentication_form = LockoutAuthenticationForm


def health(request):
    """For a load balancer or uptime monitor: confirms the app and its database answer. Shows nothing else."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:
        return JsonResponse({"status": "database unavailable"}, status=503)
    return JsonResponse({"status": "ok"})


# ---------------------------------------------------------------- two-factor sign-in
def _base(request):
    return layout_template(request)


def _next(request):
    target = request.POST.get("next") or request.GET.get("next") or "/"
    ok = url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}, require_https=request.is_secure())
    return target if ok else "/"


def _locked(request):
    """An error message while too many wrong codes have been tried, else None."""
    remaining = lockout.lock_remaining(lockout.code_key(request.user), client_ip(request))
    return None if remaining is None else f"Too many wrong codes. Try again in {lockout.minutes_text(remaining)}."


def _wrong_code(request):
    lockout.record_failure(lockout.code_key(request.user), client_ip(request))
    twofactor.audit("Two-factor code rejected", request.user.get_username())


def _grouped(secret):
    return " ".join(secret[i:i + 4] for i in range(0, len(secret), 4))


@login_required
@never_cache
def verify(request):
    user = request.user
    if not twofactor.is_enabled(user) or request.session.get(SESSION_KEY) == user.pk:
        return redirect(_next(request))
    error = ""
    if request.method == "POST":
        error = _locked(request) or ""
        if not error:
            how = twofactor.check_code(user, request.POST.get("code", ""))
            if how:
                lockout.clear(lockout.code_key(user))
                request.session[SESSION_KEY] = user.pk
                if how == "recovery":
                    twofactor.audit("Two-factor recovery code used", user.get_username())
                    messages.warning(request, f"You signed in with a recovery code. {twofactor.codes_left(user)} left. "
                                              "Make new ones from your account page if you are running low.")
                return redirect(_next(request))
            _wrong_code(request)
            error = "That code is not right, or it has already been used. Wait for the app to show a new one and try again."
    return render(request, "security/verify.html", {"base_template": _base(request), "error": error,
                                                    "next": _next(request)})


@login_required
@never_cache
def setup(request):
    user = request.user
    ctx = {"base_template": _base(request), "required": twofactor.policy_requires(user)}
    if twofactor.is_enabled(user):
        return render(request, "security/setup.html", {**ctx, "state": "on", "codes_left": twofactor.codes_left(user)})
    pending = twofactor.pending(user)
    error = ""
    if request.method == "POST":
        error = _locked(request) or ""
        if not error:
            codes = twofactor.confirm(user, request.POST.get("code", ""))
            if codes:
                lockout.clear(lockout.code_key(user))
                request.session[SESSION_KEY] = user.pk
                end_sessions(user, keep_key=request.session.session_key)       # anything opened earlier is now out of date
                return render(request, "security/setup.html", {**ctx, "state": "codes", "codes": codes})
            _wrong_code(request)
            error = "That code is not right. Check that your phone's clock is correct and try the next code."
    uri = twofactor.uri(pending.secret, user.get_username())
    return render(request, "security/setup.html", {**ctx, "state": "setup", "error": error, "key": _grouped(pending.secret),
                                                   "uri": uri, "qr": qr.svg(uri)})


def _reconfirmed(request):
    """Changing two-factor needs the password and a current code, so a borrowed signed-in session cannot do it."""
    error = _locked(request)
    if error:
        return error
    if not request.user.check_password(request.POST.get("password", "")):
        _wrong_code(request)
        return "That password is not right."
    if not twofactor.check_code(request.user, request.POST.get("code", "")):
        _wrong_code(request)
        return "That code is not right, or it has already been used."
    lockout.clear(lockout.code_key(request.user))
    return None


@login_required
@never_cache
@require_POST
def regenerate(request):
    error = _reconfirmed(request)
    if error:
        messages.error(request, error)
        return redirect("security:setup")
    codes = twofactor.regenerate_codes(request.user)
    twofactor.audit("Two-factor recovery codes regenerated", request.user.get_username())
    return render(request, "security/setup.html", {"base_template": _base(request), "state": "codes", "codes": codes,
                                                   "required": twofactor.policy_requires(request.user)})


@login_required
@never_cache
@require_POST
def disable(request):
    if twofactor.policy_requires(request.user):
        messages.error(request, "Two-factor sign-in is compulsory for administrators, so it cannot be switched off.")
        return redirect("security:setup")
    error = _reconfirmed(request)
    if error:
        messages.error(request, error)
        return redirect("security:setup")
    twofactor.disable(request.user)
    request.session.pop(SESSION_KEY, None)
    messages.success(request, "Two-factor sign-in is switched off.")
    return redirect("security:setup")


@login_required
@admin_only
def users(request):
    people = (get_user_model().objects.select_related("two_factor")
              .annotate(codes=Count("recovery_codes", filter=Q(recovery_codes__used_at__isnull=True)))
              .order_by("-is_superuser", "username"))
    rows = []
    for person in people:
        tf = getattr(person, "two_factor", None)
        rows.append({"user": person, "state": "on" if tf and tf.confirmed else "pending" if tf else "off",
                     "role": "Administrator" if person.is_superuser else "Employee portal"
                     if hasattr(person, "employee_account") and not person.company_access.exists() else "Standard user"})
    return render(request, "security/users.html", {"base_template": "payroll/base.html", "rows": rows,
                                                   "required": getattr(settings, "TWO_FACTOR_REQUIRED_FOR_ADMINS", False)})


@login_required
@admin_only
@require_POST
def reset(request, user_id):
    person = get_object_or_404(get_user_model(), pk=user_id)
    if person == request.user:
        messages.error(request, "Use your own account page to change your own two-factor sign-in.")
    else:
        twofactor.disable(person)
        end_sessions(person)                                                    # they have to sign in again from scratch
        messages.success(request, f"Two-factor sign-in was reset for {person.get_username()}. "
                                  "They can sign in with their password and set it up again.")
    return redirect("security:users")
