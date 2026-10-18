"""Exit rules: how the final month is paid, the leave payout, the preview, and completing an exit."""
import calendar
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from django.apps import apps
from django.db import transaction
from django.utils import timezone

from payroll import integrations as payroll_integrations
from payroll import services as payroll_services
from payroll.engine import DEDUCTION, EARNING
from payroll.models import Payslip, PayrollRun, SalaryElement

from .models import ClearanceItem, Exit, SettlementLine

FINAL = ("APPROVED", "PAID")
DEFAULT_CLEARANCE = [
    "Return laptop and other equipment", "Return ID card, keys and access cards",
    "Hand over work and documents", "Remove system and email access",
    "Exit interview held", "Final pay agreed with the employee",
]


def _money(x):
    return Decimal(x).quantize(Decimal("0.01"), ROUND_HALF_UP)


# ---------------------------------------------------------------- what payroll asks
@dataclass
class Terms:
    """How one payroll run must treat an employee who is leaving."""
    skip: bool = False                  # they left in an earlier month: no payslip
    final: bool = False                 # this is their last month
    factor: Decimal = Decimal("1")      # share of the month worked
    days_worked: int = 0
    days_in_month: int = 0
    extras: list = field(default_factory=list)        # (code, name, kind, amount, taxable)


def terms_for(employee, run):
    ex = Exit.objects.filter(employee=employee, status=Exit.Status.OPEN).first()
    if ex is None:
        return None
    last = ex.last_working_day
    if (run.year, run.month) < (last.year, last.month):
        return None
    if (run.year, run.month) > (last.year, last.month):
        return Terms(skip=True)
    days = calendar.monthrange(last.year, last.month)[1]
    extras = [(f"SETTLE{l.pk}", l.description, l.kind, l.amount, l.taxable if l.kind == EARNING else False)
              for l in ex.lines.all()]
    return Terms(final=True, factor=Decimal(last.day) / Decimal(days), days_worked=last.day,
                 days_in_month=days, extras=extras)


# ---------------------------------------------------------------- state of an exit
def first_open_month(company):
    """The first payroll month whose pay has not been approved yet, or None when no month is locked at all."""
    run = PayrollRun.objects.filter(company=company, status__in=FINAL).order_by("-year", "-month").first()
    if run is None:
        return None
    return date(run.year + (run.month == 12), run.month % 12 + 1, 1)


def final_run(ex):
    last = ex.last_working_day
    return PayrollRun.objects.filter(company=ex.company, year=last.year, month=last.month).first()


def final_payslip(ex):
    last = ex.last_working_day
    return Payslip.objects.filter(employee=ex.employee, run__company=ex.company, run__year=last.year,
                                  run__month=last.month, run__status__in=FINAL).first()


def is_locked(ex):
    """Once the final month is approved, the dates and the settlement lines are part of the record."""
    return ex.status != Exit.Status.OPEN or final_payslip(ex) is not None


def blockers(ex):
    """What still stands between this exit and completion."""
    problems = []
    if final_payslip(ex) is None:
        problems.append("The payroll run for the final month has not been approved yet.")
    undone = ex.items.filter(is_done=False).count()
    if undone:
        problems.append(f"{undone} clearance item{'s' if undone != 1 else ''} not done.")
    return problems


# ---------------------------------------------------------------- changing an exit
@transaction.atomic
def start_exit(employee, reason, last_working_day, notice_date=None, notes="", user=None):
    ex = Exit.objects.create(company=employee.company, employee=employee, reason=reason,
                             last_working_day=last_working_day, notice_date=notice_date, notes=notes,
                             created_by=user)
    ClearanceItem.objects.bulk_create([ClearanceItem(exit=ex, name=n) for n in DEFAULT_CLEARANCE])
    return ex


@transaction.atomic
def complete(ex, user):
    """Close the exit and take the employee off payroll."""
    if ex.status != Exit.Status.OPEN:
        raise ValueError("This exit is not in progress.")
    problems = blockers(ex)
    if problems:
        raise ValueError(" ".join(problems))
    ex.status, ex.completed_by, ex.completed_at = Exit.Status.COMPLETED, user, timezone.now()
    ex.save()
    employee = ex.employee
    employee.is_active = False
    employee.save(update_fields=["is_active"])


@transaction.atomic
def cancel(ex):
    if ex.status != Exit.Status.OPEN:
        raise ValueError("Only an exit in progress can be cancelled.")
    if final_payslip(ex) is not None:
        raise ValueError("The final month has already been approved, so this exit can no longer be cancelled.")
    ex.status = Exit.Status.CANCELLED
    ex.save()


# ---------------------------------------------------------------- leave payout
def leave_payouts(ex):
    """Unused annual leave worth paying out: [(leave type, accrued, taken, days to pay, amount)].

    Annual leave is the leave that earns the leave allowance. The entitlement accrues through the year up to the
    last working day, and each day is paid at the monthly gross divided by the absence-day rule (30 by default).
    """
    if not apps.is_installed("leave"):
        return []
    from leave import services as leave_services
    from leave.models import LeaveType
    last, emp = ex.last_working_day, ex.employee
    year_days = 366 if calendar.isleap(last.year) else 365
    share = Decimal(last.timetuple().tm_yday) / Decimal(year_days)
    rules = payroll_services.load_rules(ex.company, date(last.year, last.month, 1))
    if rules.absence_divisor <= 0:
        return []
    gross = payroll_integrations.monthly_gross(emp, PayrollRun(year=last.year, month=last.month))
    found = []
    for lt in LeaveType.objects.filter(company=ex.company, is_active=True, is_paid=True, earns_allowance=True):
        entitled = leave_services.entitled_days(emp, lt, last.year)
        if entitled is None:
            continue
        taken, _ = leave_services.usage(emp, lt, last.year)
        accrued = (entitled * share).quantize(Decimal("0.1"), ROUND_HALF_UP)
        days = accrued - taken
        if days > 0:
            found.append((lt, accrued, taken, days, _money(gross / rules.absence_divisor * days)))
    return found


@transaction.atomic
def add_suggestions(ex):
    """Add the leave payout as an editable line. Running it twice does not duplicate anything."""
    added = []
    for lt, _, _, days, amount in leave_payouts(ex):
        description = f"Unused {lt.name}: {days.normalize():f} day(s)"[:100]
        if not ex.lines.filter(description=description).exists():
            added.append(SettlementLine.objects.create(exit=ex, kind=EARNING, description=description,
                                                       amount=amount, taxable=True))
    return added


# ---------------------------------------------------------------- preview of the final payslip
def preview(ex):
    """What the final payslip will look like, worked out by payroll itself and then thrown away.

    Returns a dict, or {"error": text} when payroll cannot work it out yet.
    """
    last = ex.last_working_day
    rules = payroll_services.load_rules(ex.company, date(last.year, last.month, 1))
    elements = [payroll_services.to_engine(e) for e in SalaryElement.objects.filter(company=ex.company, is_active=True)]
    if not elements:
        return {"error": "Add at least one salary element in payroll setup first."}
    try:
        with transaction.atomic():
            run = final_run(ex) or PayrollRun.objects.create(company=ex.company, year=last.year, month=last.month)
            pay = payroll_services.compute_employee(ex.employee, run, rules, elements)
            transaction.set_rollback(True)                # a temporary run is never kept
    except ValueError as exc:
        return {"error": str(exc)}
    res = pay.res
    lines = [(l.name, l.kind, l.amount) for l in res.lines]
    return {"lines": lines, "gross": res.gross, "earnings": res.total_earnings, "deductions": res.total_deductions,
            "paye": res.paye, "net": res.net_pay, "prorata_cut": pay.prorata_cut, "terms": pay.terms,
            "worked_days": pay.worked_days, "days_in_month": pay.days_in_month,
            "owes": res.net_pay < 0}
