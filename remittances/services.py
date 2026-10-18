"""Amounts due, deadlines, schedules and payment status for PAYE and pension."""
from collections import defaultdict
from datetime import date
from decimal import Decimal

from django.db.models import Sum

from payroll.models import Payslip, PayslipLine, PayrollRun

from . import integrations
from .models import Authority, RemittancePayment, RemittanceSetting

ZERO = Decimal("0.00")
# Starting points only. Each company can change them on the Deadlines page, and should confirm them.
DEFAULT_DUE_DAY = {Authority.PAYE: 10, Authority.PENSION: 10}
FINAL_STATUSES = ("APPROVED", "PAID")
NOT_SET = "Not set"


def due_days(company):
    days = dict(DEFAULT_DUE_DAY)
    days.update({s.authority: s.due_day for s in RemittanceSetting.objects.filter(company=company)})
    return days


def due_date(run, day):
    """The chosen day of the month after the payroll month."""
    year, month = (run.year + 1, 1) if run.month == 12 else (run.year, run.month + 1)
    return date(year, month, day)


def runs_in_scope(company):
    return PayrollRun.objects.filter(company=company, status__in=FINAL_STATUSES).order_by("-year", "-month")


def amounts_due(runs):
    """{(run id, authority): amount owed}, read from the payslips of approved or paid runs."""
    due = defaultdict(lambda: ZERO)
    for row in Payslip.objects.filter(run__in=runs).values("run_id").annotate(t=Sum("paye")):
        due[(row["run_id"], Authority.PAYE)] = row["t"] or ZERO
    lines = (PayslipLine.objects.filter(payslip__run__in=runs, code__in=["PENSION_EE", "PENSION_ER"])
             .values("payslip__run_id").annotate(t=Sum("amount")))
    for row in lines:
        due[(row["payslip__run_id"], Authority.PENSION)] = row["t"] or ZERO
    return due


def amounts_paid(runs):
    paid = defaultdict(lambda: ZERO)
    for row in (RemittancePayment.objects.filter(run__in=runs).values("run_id", "authority")
                .annotate(t=Sum("amount"))):
        paid[(row["run_id"], row["authority"])] = row["t"]
    return paid


def status_of(due, paid, deadline, today):
    if due <= 0:
        return "Nothing due"
    if paid >= due:
        return "Paid"
    if today > deadline:
        return "Overdue"
    return "Part-paid" if paid > 0 else "Outstanding"


def position(company, today=None, limit=12):
    """One row per run and authority for the most recent approved or paid runs."""
    today = today or date.today()
    runs = list(runs_in_scope(company)[:limit])
    days, due, paid = due_days(company), amounts_due(runs), amounts_paid(runs)
    rows = []
    for run in runs:
        for authority in (Authority.PAYE, Authority.PENSION):
            key = (run.pk, authority)
            deadline = due_date(run, days[authority])
            rows.append({
                "run": run, "authority": authority, "label": Authority(authority).label,
                "due": due[key], "paid": paid[key], "outstanding": max(due[key] - paid[key], ZERO),
                "deadline": deadline, "status": status_of(due[key], paid[key], deadline, today),
            })
    return rows


def summary(rows):
    open_rows = [r for r in rows if r["outstanding"] > 0]
    return {"outstanding": sum((r["outstanding"] for r in open_rows), ZERO),
            "overdue": [r for r in open_rows if r["status"] == "Overdue"]}


def outstanding(run, authority):
    due = amounts_due([run])[(run.pk, authority)]
    paid = amounts_paid([run])[(run.pk, authority)]
    return max(due - paid, ZERO)


def paye_schedule(run):
    """Every employee on the run with the tax deducted, ready to send to the tax authority."""
    slips = list(run.payslips.select_related("employee").order_by("employee__employee_no"))
    ids = integrations.identifiers(s.employee_id for s in slips)
    rows = []
    for s in slips:
        profile = ids.get(s.employee_id)
        rows.append({"employee": s.employee, "tin": profile.tax_id if profile else "",
                     "gross": s.gross, "paye": s.paye})
    return {"rows": rows, "total": sum((r["paye"] for r in rows), ZERO),
            "missing_ids": sum(1 for r in rows if not r["tin"]), "hr": integrations.hr_installed()}


def pension_schedule(run):
    """Employee and employer pension per person, grouped by pension administrator (one payment each)."""
    lines = PayslipLine.objects.filter(payslip__run=run, code__in=["PENSION_EE", "PENSION_ER"])
    per_slip = defaultdict(lambda: {"ee": ZERO, "er": ZERO})
    for l in lines:
        per_slip[l.payslip_id]["ee" if l.code == "PENSION_EE" else "er"] += l.amount
    slips = (run.payslips.filter(pk__in=list(per_slip)).select_related("employee").order_by("employee__employee_no"))
    slips = list(slips)
    ids = integrations.identifiers(s.employee_id for s in slips)
    groups = defaultdict(list)
    for s in slips:
        profile = ids.get(s.employee_id)
        pfa = (profile.pension_administrator.strip() if profile else "") or NOT_SET
        amounts = per_slip[s.pk]
        groups[pfa].append({"employee": s.employee, "rsa_pin": profile.rsa_pin if profile else "",
                            "ee": amounts["ee"], "er": amounts["er"], "total": amounts["ee"] + amounts["er"]})
    result = []
    for pfa in sorted(groups, key=lambda p: (p == NOT_SET, p.lower())):
        rows = groups[pfa]
        result.append({"pfa": pfa, "rows": rows, "ee": sum((r["ee"] for r in rows), ZERO),
                       "er": sum((r["er"] for r in rows), ZERO), "total": sum((r["total"] for r in rows), ZERO)})
    return {"groups": result, "ee": sum((g["ee"] for g in result), ZERO), "er": sum((g["er"] for g in result), ZERO),
            "total": sum((g["total"] for g in result), ZERO),
            "missing_ids": sum(1 for g in result for r in g["rows"] if not r["rsa_pin"] or g["pfa"] == NOT_SET),
            "hr": integrations.hr_installed()}
