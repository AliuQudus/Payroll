"""Leave rules: day counting, balances, overlap and approval checks, notifications, and the payroll hooks."""
import calendar
import smtplib
from datetime import date, timedelta
from decimal import Decimal

from django.core.mail import EmailMessage
from django.utils import timezone

from payroll.models import Employee
from payroll import access
from payroll.services import explain_mail_error, open_mail_connection

from .models import LeaveAllowanceApplication, LeaveEntitlement, LeaveRequest, LeaveType, PublicHoliday

ZERO = Decimal("0")
LIVE = ["APPROVED", "PENDING"]


def holiday_dates(company_id):
    return set(PublicHoliday.objects.filter(company_id=company_id).values_list("date", flat=True))


def count_days(start, end, working_only, holidays):
    """Days from start to end inclusive; weekends and public holidays are skipped for working-day types."""
    if end < start:
        return ZERO
    total, day = 0, start
    while day <= end:
        if not working_only or (day.weekday() < 5 and day not in holidays):
            total += 1
        day += timedelta(days=1)
    return Decimal(total)


def days_within(req, window_start, window_end, holidays):
    """Days of a leave request that fall inside a window (a month or a year)."""
    return count_days(max(req.start_date, window_start), min(req.end_date, window_end),
                      req.leave_type.working_days_only, holidays)


def entitled_days(employee, leave_type, year):
    """Days allowed for the year. None means no limit."""
    override = LeaveEntitlement.objects.filter(employee=employee, leave_type=leave_type, year=year).first()
    if override:
        return override.days
    return Decimal(leave_type.days_per_year) if leave_type.days_per_year else None


def usage(employee, leave_type, year, exclude_pk=None):
    """(approved days, pending days) falling in the year."""
    first, last = date(year, 1, 1), date(year, 12, 31)
    holidays = holiday_dates(employee.company_id)
    qs = LeaveRequest.objects.filter(employee=employee, leave_type=leave_type, status__in=LIVE,
                                     start_date__lte=last, end_date__gte=first).select_related("leave_type")
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)
    taken = pending = ZERO
    for req in qs:
        days = days_within(req, first, last, holidays)
        if req.status == "APPROVED":
            taken += days
        else:
            pending += days
    return taken, pending


def balance(employee, leave_type, year, exclude_pk=None):
    entitled = entitled_days(employee, leave_type, year)
    taken, pending = usage(employee, leave_type, year, exclude_pk)
    remaining = None if entitled is None else entitled - taken - pending
    return {"entitled": entitled, "taken": taken, "pending": pending, "remaining": remaining}


def year_report(company, year):
    """Everyone's balance for every active leave type: (types, [(employee, [cell, ...])])."""
    first, last = date(year, 1, 1), date(year, 12, 31)
    holidays = holiday_dates(company.pk)
    types = list(LeaveType.objects.filter(company=company, is_active=True))
    employees = Employee.objects.filter(company=company, is_active=True).order_by("employee_no")
    overrides = {(e.employee_id, e.leave_type_id): e.days
                 for e in LeaveEntitlement.objects.filter(company=company, year=year)}
    used = {}
    for req in LeaveRequest.objects.filter(company=company, status__in=LIVE, start_date__lte=last,
                                           end_date__gte=first).select_related("leave_type"):
        days = days_within(req, first, last, holidays)
        taken, pending = used.get((req.employee_id, req.leave_type_id), (ZERO, ZERO))
        used[(req.employee_id, req.leave_type_id)] = (taken + days, pending) if req.status == "APPROVED" \
            else (taken, pending + days)
    rows = []
    for emp in employees:
        cells = []
        for t in types:
            entitled = overrides.get((emp.pk, t.pk), Decimal(t.days_per_year) if t.days_per_year else None)
            taken, pending = used.get((emp.pk, t.pk), (ZERO, ZERO))
            cells.append({"entitled": entitled, "taken": taken, "pending": pending,
                          "remaining": None if entitled is None else entitled - taken - pending})
        rows.append((emp, cells))
    return types, rows


def overlapping(employee, start, end, exclude_pk=None):
    qs = LeaveRequest.objects.filter(employee=employee, status__in=LIVE, start_date__lte=end,
                                     end_date__gte=start).select_related("leave_type")
    return qs.exclude(pk=exclude_pk) if exclude_pk else qs


def check_request(employee, leave_type, start, end, exclude_pk=None):
    """Returns (days, error). The error is None when the request can go ahead."""
    if end < start:
        return ZERO, "The end date cannot be before the start date."
    days = count_days(start, end, leave_type.working_days_only, holiday_dates(employee.company_id))
    if days <= 0:
        return days, "That period has no leave days in it (it is all weekends or public holidays)."
    clash = overlapping(employee, start, end, exclude_pk).first()
    if clash:
        return days, (f"This overlaps another request ({clash.leave_type.name}, "
                      f"{clash.start_date:%d %b %Y} to {clash.end_date:%d %b %Y}).")
    holidays = holiday_dates(employee.company_id)
    for year in range(start.year, end.year + 1):
        bal = balance(employee, leave_type, year, exclude_pk)
        if bal["remaining"] is None:
            continue
        part = count_days(max(start, date(year, 1, 1)), min(end, date(year, 12, 31)),
                          leave_type.working_days_only, holidays)
        if part > bal["remaining"]:
            return days, (f"Not enough {leave_type.name} left for {year}: "
                          f"{bal['remaining'].normalize():f} day(s) available, {part.normalize():f} requested.")
    return days, None


def decide(req, action, user, note=""):
    """Approve, reject or cancel a leave request."""
    if action in ("approve", "reject"):
        if req.status != "PENDING":
            raise ValueError("Only pending requests can be approved or rejected.")
        access.check_can_approve(user, req.company, "LEAVE", "leave requests")
        if req.requested_by_id == user.id and not user.is_superuser:
            raise ValueError("A different user must approve or reject this request.")
        if action == "approve":
            _, error = check_request(req.employee, req.leave_type, req.start_date, req.end_date, exclude_pk=req.pk)
            if error:
                raise ValueError(error)
        req.status = "APPROVED" if action == "approve" else "REJECTED"
    elif action == "cancel":
        if req.status not in ("PENDING", "APPROVED"):
            raise ValueError("This request is already closed.")
        if req.status == "APPROVED":
            access.check_can_approve(user, req.company, "LEAVE", "leave requests")
        if req.status == "APPROVED" and req.allowance_paid:
            raise ValueError("The leave allowance for this leave was paid in an approved payroll run, "
                             "so it cannot be cancelled.")
        req.status = "CANCELLED"
    else:
        raise ValueError("Unknown action.")
    req.decided_by, req.decided_at, req.decision_note = user, timezone.now(), note[:300]
    req.save()


# ---------------------------------------------------------------- hooks used by payroll
def unpaid_days_in_month(employee, year, month):
    """Days of approved unpaid leave that fall in the month."""
    first, last = date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
    holidays = holiday_dates(employee.company_id)
    qs = LeaveRequest.objects.filter(employee=employee, status="APPROVED", leave_type__is_paid=False,
                                     start_date__lte=last, end_date__gte=first).select_related("leave_type")
    return sum((days_within(r, first, last, holidays) for r in qs), ZERO)


def allowance_due(employee, run):
    """The approved leave that earns the leave allowance in this run, if any.

    One allowance is paid per leave year, in the first run on or after the month the leave starts.
    """
    for req in LeaveRequest.objects.filter(employee=employee, status="APPROVED",
                                           leave_type__earns_allowance=True).order_by("start_date"):
        if (req.start_date.year, req.start_date.month) > (run.year, run.month):
            break
        already_paid = LeaveAllowanceApplication.objects.filter(
            leave_request__employee=employee, leave_request__start_date__year=req.start_date.year,
            payslip__run__status__in=["APPROVED", "PAID"]).exclude(payslip__run=run).exists()
        if not already_paid:
            return req
    return None


def on_leave(employee, start, end):
    """An approved leave that overlaps the dates, if any."""
    return LeaveRequest.objects.filter(employee=employee, status="APPROVED", start_date__lte=end,
                                       end_date__gte=start).select_related("leave_type").first()


def record_allowances(payslip, applied):
    """Remember which leave each allowance on a payslip was paid for. applied = [(leave request id, amount)]."""
    LeaveAllowanceApplication.objects.bulk_create(
        [LeaveAllowanceApplication(leave_request_id=i, payslip=payslip, amount=amount) for i, amount in applied])


def allowance_pending(run):
    """Employees with leave that earns the allowance, but whose payslip in this run did not include it."""
    out = []
    for emp in Employee.objects.filter(company=run.company, is_active=True):
        due = allowance_due(emp, run)
        if due and not LeaveAllowanceApplication.objects.filter(leave_request=due, payslip__run=run).exists():
            out.append(emp)
    return out


def notify(req):
    """Email the employee about a decision. True = sent, False = nothing to send, text = what went wrong."""
    employee, company = req.employee, req.company
    if not employee.email or not company.email:
        return False
    verdict = {"APPROVED": "approved", "REJECTED": "rejected", "CANCELLED": "cancelled"}.get(req.status)
    if not verdict:
        return False
    body = (f"Hello {employee.first_name},\n\nYour {req.leave_type.name} request from {req.start_date:%d %b %Y} "
            f"to {req.end_date:%d %b %Y} ({req.days.normalize():f} day(s)) has been {verdict}.")
    if req.decision_note:
        body += f"\n\nNote: {req.decision_note}"
    body += f"\n\n{company.name}"
    try:
        connection = open_mail_connection(company)
    except ValueError as exc:
        return str(exc)
    try:
        EmailMessage(subject=f"Your leave request was {verdict}", body=body,
                     from_email=f"{company.name} <{company.email}>", to=[employee.email],
                     connection=connection).send()
    except (smtplib.SMTPException, OSError) as exc:
        return explain_mail_error(exc)
    except Exception as exc:
        return f"unexpected error ({type(exc).__name__}): {exc}"
    finally:
        connection.close()
    return True
