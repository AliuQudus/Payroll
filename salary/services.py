"""Salary history rules: which gross applies in a month, and the back pay owed when history is changed."""
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import Q, Sum

from payroll.models import Employee, Payslip, PayslipLine, PayrollRun

from .models import ArrearsApplication, SalaryBasis, SalaryRevision

ZERO = Decimal("0.00")
FINAL = ("APPROVED", "PAID")


def _money(x):
    return Decimal(x).quantize(Decimal("0.01"), ROUND_HALF_UP)


def add_months(first_of_month, n):
    index = first_of_month.year * 12 + first_of_month.month - 1 + n
    return date(index // 12, index % 12 + 1, 1)


# ---------------------------------------------------------------- which gross applies
def effective_gross(revisions, year, month):
    """The gross in force for a payroll month. Before the first revision, the first revision's gross applies."""
    target = date(year, month, 1)
    chosen = revisions[0]
    for rev in revisions:                       # revisions are sorted oldest first
        if rev.effective_from <= target:
            chosen = rev
    return chosen.gross_monthly


def _revisions(employee):
    return list(SalaryRevision.objects.filter(employee=employee).order_by("effective_from"))


def has_history(employee):
    return employee.pk is not None and SalaryRevision.objects.filter(employee=employee).exists()


def gross_for(employee, run):
    """The salary history's gross for this run's month, or None when the employee has no history."""
    revs = _revisions(employee)
    return effective_gross(revs, run.year, run.month) if revs else None


def current_revision(employee, today=None):
    today = today or date.today()
    return (SalaryRevision.objects.filter(employee=employee, effective_from__lte=today)
            .select_related("grade").order_by("-effective_from").first())


def sync_current(employee, today=None):
    """Keep Employee.gross_monthly equal to today's gross, so lists and the calculator stay right."""
    current = current_revision(employee, today)
    if current and employee.gross_monthly != current.gross_monthly:
        Employee.objects.filter(pk=employee.pk).update(gross_monthly=current.gross_monthly)   # no signals
        employee.gross_monthly = current.gross_monthly


def refresh(company):
    """Called before a run is generated: a change dated since the last run has now come into force."""
    for emp in Employee.objects.filter(company=company, salary_revisions__isnull=False).distinct():
        sync_current(emp)


# ---------------------------------------------------------------- which months are already paid
def last_final_month(company):
    run = (PayrollRun.objects.filter(company=company, status__in=FINAL).order_by("-year", "-month").first())
    return date(run.year, run.month, 1) if run else None


def first_open_month(company, today=None):
    """The first payroll month whose pay has not been approved yet."""
    last = last_final_month(company)
    return add_months(last, 1) if last else (today or date.today()).replace(day=1)


def is_locked(revision):
    last = last_final_month(revision.company)
    return bool(last and revision.effective_from <= last)


# ---------------------------------------------------------------- changing the history
def backfill_basis(employee):
    """Work out the basis of payslips made before this module was used: gross plus any absence reduction."""
    slips = (Payslip.objects.filter(employee=employee, salary_basis__isnull=True)
             .annotate(cut=Sum("lines__amount", filter=Q(lines__code="ABSENCE"))))
    SalaryBasis.objects.bulk_create(
        [SalaryBasis(payslip=s, monthly_gross=s.gross + (s.cut or ZERO)) for s in slips])


def _paid_history(employee):
    """[(first of month, gross)] for each change in what an employee was actually paid on, from payslips."""
    history = []
    bases = (SalaryBasis.objects.filter(payslip__employee=employee, payslip__run__status__in=FINAL)
             .select_related("payslip__run").order_by("payslip__run__year", "payslip__run__month"))
    for basis in bases:
        run = basis.payslip.run
        if not history or history[-1][1] != basis.monthly_gross:
            history.append((date(run.year, run.month, 1), basis.monthly_gross))
    return history


def ensure_opening(employee, new_effective, user=None):
    """The first time history is recorded, rebuild it from what was really paid.

    Copying today's salary back over old months would invent back pay for every month before an earlier raise,
    so each change visible in the approved payslips becomes a revision of its own.
    """
    if SalaryRevision.objects.filter(employee=employee).exists():
        return []
    backfill_basis(employee)
    history = [h for h in _paid_history(employee) if h[0] < new_effective]     # the new change overrides the rest
    if not history:
        paid = _paid_history(employee)
        start = add_months(new_effective, -1)
        if apps_hr():
            from hr.models import EmployeeProfile
            profile = EmployeeProfile.objects.filter(employee=employee).first()
            if profile:
                start = min(start, profile.hire_date.replace(day=1))
        history = [(start, paid[0][1] if paid else employee.gross_monthly)]
    open_month = first_open_month(employee.company)
    if history[-1][1] != employee.gross_monthly and history[-1][0] < open_month < new_effective:
        history.append((open_month, employee.gross_monthly))                  # salary edited since the last run
    created = []
    for i, (month, gross) in enumerate(history):
        created.append(SalaryRevision.objects.create(
            company=employee.company, employee=employee, effective_from=month, gross_monthly=gross,
            reason=SalaryRevision.Reason.OPENING if i == 0 else SalaryRevision.Reason.OTHER,
            note="Salary on file when history was first recorded." if i == 0 else "Rebuilt from payslips already approved.",
            created_by=user))
    return created


def apps_hr():
    from django.apps import apps
    return apps.is_installed("hr")


@transaction.atomic
def add_revision(employee, effective_from, gross, reason, grade=None, note="", user=None):
    effective_from = effective_from.replace(day=1)
    ensure_opening(employee, effective_from, user)
    revision = SalaryRevision.objects.create(
        company=employee.company, employee=employee, effective_from=effective_from, gross_monthly=_money(gross),
        reason=reason, grade=grade, note=note, created_by=user)
    backfill_basis(employee)
    sync_current(employee)
    return revision


@transaction.atomic
def delete_revision(revision):
    if is_locked(revision):
        raise ValueError("That change falls in a month that has already been approved, so it is part of the "
                         "record. Add a correcting change from a later month instead.")
    others = SalaryRevision.objects.filter(employee=revision.employee).exclude(pk=revision.pk)
    if revision.reason == SalaryRevision.Reason.OPENING and others.exists():
        raise ValueError("The opening salary cannot be deleted while later changes exist.")
    employee = revision.employee
    revision.delete()
    sync_current(employee)


# ---------------------------------------------------------------- back pay
def arrears_for(employee, company, year, month, exclude_run=None):
    """Back pay owed in the payroll of (year, month), from earlier approved or paid months paid on a lower gross.

    Returns (amount owed, [(basis id, amount)], overpaid). Overpayments are reported but never taken back.
    """
    revs = _revisions(employee)
    if not revs:
        return ZERO, [], ZERO
    bases = (SalaryBasis.objects.filter(payslip__employee=employee, payslip__run__company=company,
                                        payslip__run__status__in=FINAL)
             .select_related("payslip__run"))
    if exclude_run is not None:
        bases = bases.exclude(payslip__run=exclude_run)
    owed, applied, overpaid = ZERO, [], ZERO
    for basis in bases:
        run = basis.payslip.run
        if (run.year, run.month) >= (year, month):
            continue
        paid_back = (basis.arrears.exclude(payslip__run=exclude_run) if exclude_run else basis.arrears.all()
                     ).aggregate(t=Sum("amount"))["t"] or ZERO
        gap = effective_gross(revs, run.year, run.month) - basis.monthly_gross - paid_back
        if gap > 0:
            owed += gap
            applied.append((basis.pk, gap))
        elif gap < 0:
            overpaid += -gap
    return owed, applied, overpaid


def arrears_items(employee, run):
    """What payroll needs for this run: the back pay to add and how it breaks down."""
    owed, applied, _ = arrears_for(employee, run.company, run.year, run.month, exclude_run=run)
    return owed, applied


def record(slip, gross, applied):
    """Remember the basis of a new payslip and which earlier shortfalls this payslip paid."""
    basis = SalaryBasis.objects.create(payslip=slip, monthly_gross=gross)
    ArrearsApplication.objects.bulk_create(
        [ArrearsApplication(basis_id=basis_id, payslip=slip, amount=amount) for basis_id, amount in applied])


def pending(company):
    """Back pay and overpayments that the next run will deal with, by employee."""
    nxt = first_open_month(company)
    rows = []
    for emp in (Employee.objects.filter(company=company, is_active=True, salary_revisions__isnull=False)
                .distinct().order_by("employee_no")):
        owed, _, over = arrears_for(emp, company, nxt.year, nxt.month)
        if owed or over:
            rows.append({"employee": emp, "owed": owed, "overpaid": over})
    return nxt, rows
