"""The only place payroll talks to the optional leave, salary, exits, portal, attendance and hr modules.

Payroll works on its own. When the leave module is installed, approved unpaid leave reduces pay and
approved leave that earns the leave allowance puts it on the payslip. When the salary module is installed,
the gross for each month comes from the salary history and back pay is added after a backdated change.
When the exits module is installed, an employee who is leaving is paid pro rata in the final month, has their loans
settled, gets their settlement lines, and is left out of later runs. Nothing else in payroll knows about these modules.
"""
from decimal import Decimal

from django.apps import apps


def _leave():
    if not apps.is_installed("leave"):
        return None
    from leave import services
    return services


def unpaid_leave_days(employee, year, month):
    svc = _leave()
    return svc.unpaid_days_in_month(employee, year, month) if svc else Decimal("0")


def allowance_due(employee, run):
    """The approved leave that earns this employee the leave allowance in this run, or None."""
    svc = _leave()
    return svc.allowance_due(employee, run) if svc else None


def record_allowances(payslip, applied):
    svc = _leave()
    if svc and applied:
        svc.record_allowances(payslip, applied)


def unpaid_allowances(run):
    """Employees whose leave earns the allowance but whose payslip in this run did not include it."""
    svc = _leave()
    return svc.allowance_pending(run) if svc else []


def _salary():
    if not apps.is_installed("salary"):
        return None
    from salary import services
    return services


def refresh_salaries(company):
    """A salary change dated since the last run has now come into force."""
    svc = _salary()
    if svc:
        svc.refresh(company)


def monthly_gross(employee, run):
    """The gross to pay this employee for the run's month: from salary history if there is any."""
    svc = _salary()
    gross = svc.gross_for(employee, run) if svc else None
    return gross if gross is not None else employee.gross_monthly


def salary_arrears(employee, run):
    """(amount, breakdown) of back pay owed on earlier months paid below the salary history."""
    svc = _salary()
    return svc.arrears_items(employee, run) if svc else (Decimal("0"), [])


def record_salary(payslip, gross, arrears_applied):
    svc = _salary()
    if svc:
        svc.record(payslip, gross, arrears_applied)


def lock_gross(form, employee):
    """Once an employee has salary history, the salary is changed there, not on the employee form."""
    svc = _salary()
    if svc and employee is not None and "gross_monthly" in form.fields and svc.has_history(employee):
        field = form.fields["gross_monthly"]
        field.disabled = True
        field.help_text = "This employee has a salary history. Change the salary on the Salary page."


def exit_terms(employee, run):
    """None for an employee who is not leaving. Otherwise how this run should treat them (see exits.services.Terms)."""
    if not apps.is_installed("exits"):
        return None
    from exits import services
    return services.terms_for(employee, run)


def portal_home(user):
    """Where an employee's own login should land, or None when the portal is not installed or this is not one."""
    if not apps.is_installed("portal") or not hasattr(user, "employee_account"):
        return None
    from django.urls import reverse
    return reverse("portal:home")


def _attendance():
    if not apps.is_installed("attendance"):
        return None
    from attendance import services
    return services


def absent_days(employee, run):
    """Unpaid days of absence recorded in the attendance register for the run's month."""
    svc = _attendance()
    return svc.absent_days(employee, run) if svc else Decimal("0")


def overtime(employee, run):
    """(amount, hours, breakdown) of approved overtime that has not been paid yet."""
    svc = _attendance()
    return svc.overtime_items(employee, run) if svc else (Decimal("0"), Decimal("0"), [])


def record_overtime(payslip, applied):
    svc = _attendance()
    if svc:
        svc.record(payslip, applied)


def start_terms(employee, run):
    """None for an employee who started before this month or has no HR record. Otherwise skip=True (not yet
    started) or first_day (the day they started this month)."""
    if not apps.is_installed("hr"):
        return None
    from hr import services
    return services.start_terms(employee, run)
