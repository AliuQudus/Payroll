"""Giving employees a login, inviting them, and what they are allowed to see."""
import smtplib

from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import EmailMessage
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from payroll.models import PayrollRun, Payslip
from payroll.services import explain_mail_error, open_mail_connection

from .models import EmployeeAccount

FINAL = (PayrollRun.Status.APPROVED, PayrollRun.Status.PAID)


def has(app_name):
    """Is an optional module (hr, leave) installed? The pages for it are only offered when it is."""
    return apps.is_installed(app_name)


def mask_account(number):
    return f"{'•' * 6}{number[-4:]}" if number else ""


# ---------------------------------------------------------------- logins
def status_of(employee):
    """none, invited, active, revoked or left."""
    account = getattr(employee, "portal_account", None)
    if account is None:
        return "none"
    if not employee.is_active:
        return "left"
    if not account.user.is_active:
        return "revoked"
    return "active" if account.user.last_login else "invited"


@transaction.atomic
def grant(employee, created_by=None):
    """Create the employee's login. The email address is the username; no password exists until they set one."""
    if not employee.is_active:
        raise ValueError(f"{employee} has left, so cannot be given access.")
    email = employee.email.strip()
    if not email:
        raise ValueError(f"Add an email address to {employee} first. It becomes their username.")
    if hasattr(employee, "portal_account"):
        raise ValueError(f"{employee} already has portal access.")
    User = get_user_model()
    if User.objects.filter(username__iexact=email).exists():
        raise ValueError(f"A login named {email} already exists. Change this employee's email, or use the "
                         "existing login.")
    user = User(username=email.lower(), email=email, first_name=employee.first_name[:150],
                last_name=employee.last_name[:150], is_staff=False, is_superuser=False)
    user.set_unusable_password()
    user.save()
    return EmployeeAccount.objects.create(user=user, employee=employee, created_by=created_by)


def invite_link(user, request):
    """A one-time link to choose a password. It stops working after PASSWORD_RESET_TIMEOUT."""
    path = reverse("password_reset_confirm", kwargs={"uidb64": urlsafe_base64_encode(force_bytes(user.pk)),
                                                     "token": default_token_generator.make_token(user)})
    return request.build_absolute_uri(path)


def send_invite(account, request):
    """Email the invitation. Returns (problem or None, link): the link is for the administrator to pass on by
    hand when the email could not be sent."""
    user, employee = account.user, account.employee
    company = employee.company
    link = invite_link(user, request)
    days = max(settings.PASSWORD_RESET_TIMEOUT // 86400, 1)
    body = (f"Hello {employee.first_name},\n\n{company.name} has given you access to the employee portal, where you "
            f"can see your payslips, check your leave and request time off.\n\nChoose your password here:\n{link}\n\n"
            f"Your username is {user.get_username()}. The link works once and expires in {days} day(s); ask "
            f"for a new one if it has.\n\n{company.name}")
    try:
        connection = open_mail_connection(company)
    except ValueError as exc:
        return str(exc), link
    try:
        EmailMessage(subject=f"Your {company.name} employee portal access", body=body,
                     from_email=f"{company.name} <{company.email}>", to=[employee.email],
                     connection=connection).send()
    except (smtplib.SMTPException, OSError) as exc:
        return explain_mail_error(exc), link
    except Exception as exc:
        return f"unexpected error ({type(exc).__name__}): {exc}", link
    finally:
        connection.close()
    account.invited_at = timezone.now()
    account.save(update_fields=["invited_at"])
    return None, link


def revoke(account):
    """Switch the login off. It stops working at once, including any session already open."""
    account.user.is_active = False
    account.user.save(update_fields=["is_active"])


def restore(account):
    account.user.is_active = True
    account.user.save(update_fields=["is_active"])


# ---------------------------------------------------------------- what an employee may see
def visible_payslips(employee):
    """Only the employee's own payslips, and only once their run has been approved."""
    return (Payslip.objects.filter(employee=employee, run__status__in=FINAL)
            .select_related("run").order_by("-run__year", "-run__month"))
