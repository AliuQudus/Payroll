"""The only place remittances talks to the optional HR module, for tax and pension identifiers."""
from django.apps import apps


def hr_installed():
    return apps.is_installed("hr")


def identifiers(employee_ids):
    """{employee id: HR profile} for the given employees. Empty when the HR module is not installed."""
    if not hr_installed():
        return {}
    from hr.models import EmployeeProfile
    return {p.employee_id: p for p in EmployeeProfile.objects.filter(employee_id__in=list(employee_ids))}
