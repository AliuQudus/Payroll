"""Attendance and overtime rules: pricing, approval, what payroll should pay or deduct, the register and imports."""
import calendar
import csv
import io
from datetime import date, datetime, time
from decimal import ROUND_HALF_UP, Decimal

from django.apps import apps
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone

from payroll import access
from payroll import integrations as payroll_integrations
from payroll.models import Employee, PayrollRun

from .models import AttendanceRecord, AttendanceSetting, OvertimeApplication, OvertimeRequest

ZERO = Decimal("0.00")
MAX_HOURS = Decimal("12")
FINAL = ("APPROVED", "PAID")
ABSENCE_WEIGHT = {AttendanceRecord.Status.ABSENT: Decimal("1"), AttendanceRecord.Status.HALF_DAY: Decimal("0.5")}


def _money(x):
    return Decimal(x).quantize(Decimal("0.01"), ROUND_HALF_UP)


def _has_leave():
    return apps.is_installed("leave")


def settings_for(company):
    """The company's saved overtime rules, or the defaults when it has not set any."""
    return AttendanceSetting.objects.filter(company=company).first() or AttendanceSetting(company=company)


# ---------------------------------------------------------------- the calendar
def is_holiday(company, day):
    if not _has_leave():
        return False
    from leave.models import PublicHoliday
    return PublicHoliday.objects.filter(company=company, date=day).exists()


def day_type(company, day):
    if is_holiday(company, day):
        return OvertimeRequest.DayType.HOLIDAY
    return OvertimeRequest.DayType.WEEKEND if day.weekday() >= 5 else OvertimeRequest.DayType.WEEKDAY


def multiplier_for(setting, kind):
    return {OvertimeRequest.DayType.WEEKDAY: setting.weekday_rate, OvertimeRequest.DayType.WEEKEND: setting.weekend_rate,
            OvertimeRequest.DayType.HOLIDAY: setting.holiday_rate}[kind]


def on_leave(employee, day):
    """Approved leave covering the day, if the leave module is installed."""
    if not _has_leave():
        return None
    from leave import services as leave_services
    return leave_services.on_leave(employee, day, day)


# ---------------------------------------------------------------- overtime requests
def hourly_rate(employee, year, month, setting):
    """The hourly rate for the month: the salary in force then, times the base share, over the monthly hours."""
    gross = payroll_integrations.monthly_gross(employee, PayrollRun(year=year, month=month))
    return _money(gross * setting.base_percent / Decimal(100) / setting.hours_per_month)


def add_request(employee, day, hours, reason="", user=None):
    if not employee.is_active:
        raise ValueError(f"{employee} has left, so overtime cannot be recorded for them.")
    if day > date.today():
        raise ValueError("Overtime can only be recorded for days that have already happened.")
    if hours <= 0 or hours > MAX_HOURS:
        raise ValueError(f"Overtime on one day must be more than 0 and at most {MAX_HOURS.normalize():f} hours.")
    already = (OvertimeRequest.objects.filter(employee=employee, date=day,
                                              status__in=["PENDING", "APPROVED"]).aggregate(t=Sum("hours"))["t"] or 0)
    if already + hours > MAX_HOURS:
        raise ValueError(f"{employee} already has {already} hours of overtime on {day:%d %b %Y}. "
                         f"One day can have at most {MAX_HOURS.normalize():f}.")
    return OvertimeRequest.objects.create(
        company=employee.company, employee=employee, date=day, hours=hours, reason=reason,
        day_type=day_type(employee.company, day), requested_by=user)


def is_paid(req):
    """Paid once an approved or paid payroll run has taken it."""
    return OvertimeApplication.objects.filter(request=req, payslip__run__status__in=FINAL).exists()


@transaction.atomic
def decide(req, action, user, note=""):
    """Approve, reject or cancel an overtime request, under the same rules as leave."""
    if action in ("approve", "reject"):
        if req.status != OvertimeRequest.Status.PENDING:
            raise ValueError("Only requests waiting for approval can be approved or rejected.")
        access.check_can_approve(user, req.company, "OVERTIME", "overtime requests")
        if req.requested_by_id == user.id and not user.is_superuser:
            raise ValueError("A different user must approve or reject this request.")
        if action == "approve":
            setting = settings_for(req.company)
            rate = hourly_rate(req.employee, req.date.year, req.date.month, setting)
            req.hourly_rate, req.multiplier = rate, multiplier_for(setting, req.day_type)
            req.amount = _money(rate * req.hours * req.multiplier)
        req.status = OvertimeRequest.Status.APPROVED if action == "approve" else OvertimeRequest.Status.REJECTED
    elif action == "cancel":
        if req.status not in (OvertimeRequest.Status.PENDING, OvertimeRequest.Status.APPROVED):
            raise ValueError("This request is already closed.")
        if req.status == OvertimeRequest.Status.APPROVED:
            access.check_can_approve(user, req.company, "OVERTIME", "overtime requests")
            if is_paid(req):
                raise ValueError("This overtime was paid in an approved payroll run, so it cannot be cancelled.")
        req.status = OvertimeRequest.Status.CANCELLED
    else:
        raise ValueError("Unknown action.")
    req.decided_by, req.decided_at, req.decision_note = user, timezone.now(), note[:300]
    req.save()


# ---------------------------------------------------------------- what payroll asks for
def absent_days(employee, run):
    """Unpaid absence in the run's month: a full day counts 1, a half day 0.5.

    A day already covered by approved leave or a public holiday is not counted again.
    """
    total = Decimal("0")
    records = AttendanceRecord.objects.filter(employee=employee, date__year=run.year, date__month=run.month,
                                              status__in=list(ABSENCE_WEIGHT))
    for rec in records:
        if on_leave(employee, rec.date) or is_holiday(employee.company, rec.date):
            continue
        total += ABSENCE_WEIGHT[rec.status]
    return total


def overtime_items(employee, run):
    """Approved overtime not yet paid, for days up to the end of the run's month.

    Late approvals are picked up by the next run. Returns (amount, hours, [(request id, amount)]).
    """
    month_end = date(run.year, run.month, calendar.monthrange(run.year, run.month)[1])
    paid_elsewhere = (OvertimeApplication.objects.filter(request__employee=employee)
                      .exclude(payslip__run_id=run.pk).values_list("request_id", flat=True))
    requests = (OvertimeRequest.objects.filter(employee=employee, status=OvertimeRequest.Status.APPROVED,
                                               date__lte=month_end).exclude(pk__in=paid_elsewhere))
    amount, hours, applied = ZERO, Decimal("0"), []
    for req in requests:
        amount += req.amount
        hours += req.hours
        applied.append((req.pk, req.amount))
    return amount, hours, applied


def record(payslip, applied):
    OvertimeApplication.objects.bulk_create(
        [OvertimeApplication(request_id=pk, payslip=payslip, amount=amount) for pk, amount in applied])


# ---------------------------------------------------------------- the register
def parse_time(text):
    text = (text or "").strip()
    if not text:
        return None
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            pass
    raise ValueError(f"'{text}' is not a time. Use HH:MM, for example 08:30.")


def parse_date(text):
    text = (text or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"'{text}' is not a date. Use YYYY-MM-DD or DD/MM/YYYY.")


@transaction.atomic
def save_register(company, day, rows, user):
    """Save one day's register. rows = {employee id: {status, time_in, time_out, note}}.

    Returns {employee id: error text}; nothing is saved when there are errors. An empty status clears the day.
    """
    if day > date.today():
        return {0: "The register cannot be filled in for a day that has not happened yet."}
    staff = {e.pk: e for e in Employee.objects.filter(company=company, is_active=True, pk__in=list(rows))}
    errors, clean = {}, {}
    for pk, row in rows.items():
        if pk not in staff:
            continue
        status = row.get("status", "")
        if not status:
            clean[pk] = None
            continue
        if status not in AttendanceRecord.Status.values:
            errors[pk] = "Choose a status from the list."
            continue
        try:
            time_in, time_out = parse_time(row.get("time_in")), parse_time(row.get("time_out"))
        except ValueError as exc:
            errors[pk] = str(exc)
            continue
        if time_in and time_out and time_out <= time_in:
            errors[pk] = "The time out must be after the time in."
            continue
        clean[pk] = dict(status=status, time_in=time_in, time_out=time_out, note=(row.get("note") or "")[:200])
    if errors:
        transaction.set_rollback(True)
        return errors
    for pk, data in clean.items():
        if data is None:
            AttendanceRecord.objects.filter(employee_id=pk, date=day).delete()
        else:
            AttendanceRecord.objects.update_or_create(employee=staff[pk], date=day, company=company,
                                                      defaults={**data, "recorded_by": user})
    return {}


STATUS_WORDS = {"present": "PRESENT", "p": "PRESENT", "late": "LATE", "l": "LATE", "absent": "ABSENT",
                "a": "ABSENT", "half": "HALF_DAY", "half_day": "HALF_DAY", "half day": "HALF_DAY", "h": "HALF_DAY"}
MAX_IMPORT_ROWS = 5000


@transaction.atomic
def import_csv(company, text, user):
    """Load attendance from CSV with the columns employee_no, date, status and optionally time_in, time_out, note.

    All or nothing: returns (created, updated) or raises ValueError listing every problem found.
    """
    reader = csv.DictReader(io.StringIO(text))
    fields = {(f or "").strip().lower() for f in (reader.fieldnames or [])}
    missing = {"employee_no", "date", "status"} - fields
    if missing:
        raise ValueError("The file needs these columns: employee_no, date, status. Missing: "
                         + ", ".join(sorted(missing)) + ".")
    staff = {e.employee_no: e for e in Employee.objects.filter(company=company, is_active=True)}
    problems, parsed = [], {}
    for line, raw in enumerate(reader, start=2):
        if line - 1 > MAX_IMPORT_ROWS:
            raise ValueError(f"The file has more than {MAX_IMPORT_ROWS} rows. Split it into smaller files.")
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        try:
            emp = staff.get(row["employee_no"])
            if emp is None:
                raise ValueError(f"no active employee numbered '{row['employee_no']}'")
            day = parse_date(row["date"])
            if day > date.today():
                raise ValueError("the date is in the future")
            status = STATUS_WORDS.get(row["status"].lower())
            if status is None:
                raise ValueError(f"'{row['status']}' is not a status (use present, late, absent or half_day)")
            time_in, time_out = parse_time(row.get("time_in")), parse_time(row.get("time_out"))
            if time_in and time_out and time_out <= time_in:
                raise ValueError("the time out must be after the time in")
            parsed[(emp.pk, day)] = (emp, dict(status=status, time_in=time_in, time_out=time_out,
                                              note=row.get("note", "")[:200]))
        except ValueError as exc:
            problems.append(f"Line {line}: {exc}.")
    if problems:
        shown = problems[:20] + ([f"...and {len(problems) - 20} more."] if len(problems) > 20 else [])
        raise ValueError("Nothing was imported. " + " ".join(shown))
    created = updated = 0
    for (_, day), (emp, data) in parsed.items():
        _, was_created = AttendanceRecord.objects.update_or_create(
            employee=emp, date=day, company=company, defaults={**data, "recorded_by": user})
        created += was_created
        updated += not was_created
    return created, updated


# ---------------------------------------------------------------- monthly summary
def month_summary(company, year, month):
    """One row per active employee: attendance counts, leave days and approved overtime for the month."""
    counts = {}
    for row in (AttendanceRecord.objects.filter(company=company, date__year=year, date__month=month)
                .values("employee_id", "status").annotate(n=Count("id"))):
        counts.setdefault(row["employee_id"], {})[row["status"]] = row["n"]
    overtime = {row["employee_id"]: row for row in
                OvertimeRequest.objects.filter(company=company, date__year=year, date__month=month,
                                               status=OvertimeRequest.Status.APPROVED)
                .values("employee_id").annotate(hours=Sum("hours"), amount=Sum("amount"))}
    first, last = date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
    holidays = set()
    if _has_leave():
        from leave import services as leave_services
        holidays = leave_services.holiday_dates(company.pk)
    rows = []
    for emp in Employee.objects.filter(company=company, is_active=True).order_by("employee_no"):
        c = counts.get(emp.pk, {})
        leave_days = ZERO
        if _has_leave():
            from leave import services as leave_services
            from leave.models import LeaveRequest
            for req in LeaveRequest.objects.filter(employee=emp, status="APPROVED", start_date__lte=last,
                                                   end_date__gte=first).select_related("leave_type"):
                leave_days += leave_services.days_within(req, first, last, holidays)
        o = overtime.get(emp.pk, {})
        rows.append({"emp": emp, "present": c.get("PRESENT", 0), "late": c.get("LATE", 0),
                     "absent": c.get("ABSENT", 0), "half": c.get("HALF_DAY", 0), "leave_days": leave_days,
                     "ot_hours": o.get("hours") or Decimal("0"), "ot_amount": o.get("amount") or ZERO})
    return rows
