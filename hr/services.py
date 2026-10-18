"""Rules for the HR module: length of service, upcoming dates and the alerts page."""
from datetime import date, timedelta
from types import SimpleNamespace

from payroll.models import Employee

from .models import EmployeeDocument, EmployeeProfile


def service_text(hired, today=None):
    """Length of service as '3 yrs 2 mos'."""
    today = today or date.today()
    if hired > today:
        return f"Starts {hired:%d %b %Y}"
    months = (today.year - hired.year) * 12 + today.month - hired.month - (1 if today.day < hired.day else 0)
    years, months = divmod(months, 12)
    parts = []
    if years:
        parts.append(f"{years} yr{'s' if years != 1 else ''}")
    if months:
        parts.append(f"{months} mo{'s' if months != 1 else ''}")
    return " ".join(parts) or "Less than a month"


def next_occurrence(day, today):
    """The next time a yearly date (birthday, anniversary) falls on or after today. 29 Feb becomes 28 Feb."""
    for year in (today.year, today.year + 1):
        try:
            candidate = day.replace(year=year)
        except ValueError:
            candidate = date(year, 2, 28)
        if candidate >= today:
            return candidate
    return day.replace(year=today.year + 1, day=28)


def alerts(company, today=None, days=30):
    """Everything that needs attention within `days` days, for active employees only."""
    today = today or date.today()
    horizon = today + timedelta(days=days)
    active = Employee.objects.filter(company=company, is_active=True)
    profiles = list(EmployeeProfile.objects.filter(employee__in=active).select_related("employee"))

    documents = [(d, (d.expiry_date - today).days) for d in
                 EmployeeDocument.objects.filter(employee__in=active, expiry_date__isnull=False,
                                                 expiry_date__lte=horizon)
                 .select_related("employee").order_by("expiry_date")]
    contracts = sorted(((p, (p.contract_end_date - today).days) for p in profiles
                        if p.contract_end_date and p.contract_end_date <= horizon), key=lambda x: x[1])
    probation = sorted(((p, (p.probation_end_date - today).days) for p in profiles
                        if p.probation_end_date and today <= p.probation_end_date <= horizon), key=lambda x: x[1])

    birthdays, anniversaries = [], []
    for p in profiles:
        if p.date_of_birth:
            when = next_occurrence(p.date_of_birth, today)
            if when <= horizon:
                birthdays.append((p, when, (when - today).days))
        when = next_occurrence(p.hire_date, today)
        if when <= horizon and when.year - p.hire_date.year >= 1:
            anniversaries.append((p, when, when.year - p.hire_date.year))
    birthdays.sort(key=lambda x: x[1])
    anniversaries.sort(key=lambda x: x[1])

    return {
        "documents": documents, "contracts": contracts, "probation": probation,
        "birthdays": birthdays, "anniversaries": anniversaries,
        "missing": list(active.filter(profile__isnull=True).order_by("employee_no")),
    }


def start_terms(employee, run):
    """How the HR hire date affects a payroll run. None when the employee started before the run's month.

    Otherwise skip is True for a month before they started, or first_day is the day they started in that month.
    """
    profile = EmployeeProfile.objects.filter(employee=employee).only("hire_date").first()
    if profile is None:
        return None
    hired = profile.hire_date
    if (run.year, run.month) < (hired.year, hired.month):
        return SimpleNamespace(skip=True, first_day=None)
    if (run.year, run.month) > (hired.year, hired.month):
        return None
    return SimpleNamespace(skip=False, first_day=hired.day)
