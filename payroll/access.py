"""Who may work in which company, and who may approve what."""
from django.contrib.auth import get_user_model
from django.db.models import Q

from .models import Approver, Company


def name_of(user):
    return user.get_full_name() or user.username


def companies_for(user):
    """Administrators work in every company; everyone else only in the ones they were given."""
    if user.is_superuser:
        return Company.objects.all()
    return Company.objects.filter(access__user=user)


def users_with_access(company):
    return (get_user_model().objects.filter(is_active=True)
            .filter(Q(is_superuser=True) | Q(company_access__company=company)).distinct().order_by("username"))


def designated(company, scope):
    return list(Approver.objects.filter(company=company, scope=scope).select_related("user"))


def approver_names(company, scope):
    return [name_of(r.user) for r in designated(company, scope) if r.user.is_active]


def can_approve(user, company, scope):
    """With nobody designated the normal rules apply. Once someone is designated, only they can approve."""
    rows = designated(company, scope)
    return not rows or any(r.user_id == user.id and r.user.is_active for r in rows)


def check_can_approve(user, company, scope, what):
    if can_approve(user, company, scope):
        return
    names = approver_names(company, scope)
    if not names:
        raise ValueError(f"Every approver designated for {what} is deactivated. An administrator must "
                         "designate an active approver on the Approvers page.")
    raise ValueError(f"Only {', '.join(names)} can approve {what} for {company.name}.")
