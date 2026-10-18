import csv
from datetime import date, timedelta

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from payroll import access
from payroll.models import Employee
from payroll.views import admin_only, company_view

from . import services
from .forms import DecisionForm, ImportForm, OvertimeForm, SettingForm
from .models import AttendanceRecord, OvertimeRequest


def _day(text):
    try:
        return services.parse_date(text) if text else date.today()
    except ValueError:
        return date.today()


def _month(text):
    try:
        year, month = (int(x) for x in (text or "").split("-"))
        return date(year, month, 1)
    except ValueError:
        return date.today().replace(day=1)


def _cell(value):
    """Stop text that starts with = + - or @ being run as a formula when the file is opened in Excel."""
    text = "" if value is None else str(value)
    return "'" + text if text and text[0] in "=+-@\t\r" else text


# ---------------------------------------------------------------- the register
@company_view
def register(request):
    day = _day(request.POST.get("date") or request.GET.get("date"))
    staff = list(Employee.objects.filter(company=request.company, is_active=True).order_by("employee_no"))
    records = {r.employee_id: r for r in AttendanceRecord.objects.filter(company=request.company, date=day)}
    errors, posted = {}, {}
    if request.method == "POST":
        posted = {e.pk: {"status": request.POST.get(f"status_{e.pk}", ""), "time_in": request.POST.get(f"in_{e.pk}", ""),
                         "time_out": request.POST.get(f"out_{e.pk}", ""), "note": request.POST.get(f"note_{e.pk}", "")}
                  for e in staff}
        errors = services.save_register(request.company, day, posted, request.user)
        if not errors:
            messages.success(request, f"Register saved for {day:%A %d %B %Y}.")
            return redirect(f"{reverse('attendance:register')}?date={day.isoformat()}")
        if 0 in errors:
            messages.error(request, errors[0])
        else:
            messages.error(request, "Nothing was saved. Fix the rows marked below.")
    fill = request.GET.get("fill") == "present"
    rows = []
    for emp in staff:
        rec = records.get(emp.pk)
        values = posted.get(emp.pk) or ({"status": rec.status, "time_in": rec.time_in.strftime("%H:%M") if rec.time_in else "",
                                        "time_out": rec.time_out.strftime("%H:%M") if rec.time_out else "",
                                        "note": rec.note} if rec else {"status": "", "time_in": "", "time_out": "", "note": ""})
        leave = services.on_leave(emp, day)
        if fill and not values["status"] and not leave:
            values = {**values, "status": "PRESENT"}
        rows.append({"emp": emp, "v": values, "error": errors.get(emp.pk), "leave": leave})
    kind = services.day_type(request.company, day)
    return render(request, "attendance/register.html", {
        "day": day, "rows": rows, "kind": kind, "statuses": AttendanceRecord.Status.choices,
        "prev": day - timedelta(days=1), "next": day + timedelta(days=1) if day < date.today() else None,
        "today": date.today(), "future": day > date.today()})


# ---------------------------------------------------------------- summary
@company_view
def summary(request):
    first = _month(request.GET.get("month"))
    rows = services.month_summary(request.company, first.year, first.month)
    nxt = (first + timedelta(days=32)).replace(day=1)
    return render(request, "attendance/summary.html", {
        "first": first, "rows": rows, "prev": (first - timedelta(days=1)).replace(day=1),
        "next": nxt if nxt <= date.today() else None, "month_param": first.strftime("%Y-%m"),
        "ot_hours": sum(r["ot_hours"] for r in rows), "ot_amount": sum(r["ot_amount"] for r in rows)})


@company_view
def summary_csv(request):
    first = _month(request.GET.get("month"))
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="attendance-{first:%Y-%m}.csv"'
    writer = csv.writer(response)
    writer.writerow(["Employee no", "Name", "Present", "Late", "Absent", "Half days absent", "Leave days",
                     "Approved overtime hours", "Approved overtime amount"])
    for r in services.month_summary(request.company, first.year, first.month):
        writer.writerow([_cell(r["emp"].employee_no), _cell(f"{r['emp'].first_name} {r['emp'].last_name}"),
                         r["present"], r["late"], r["absent"], r["half"], r["leave_days"], r["ot_hours"],
                         r["ot_amount"]])
    return response


# ---------------------------------------------------------------- import
@company_view
def import_view(request):
    form = ImportForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            created, updated = services.import_csv(request.company, form.text, request.user)
        except ValueError as exc:
            form.add_error("file", str(exc))
        else:
            messages.success(request, f"Imported: {created} new record(s), {updated} updated.")
            return redirect("attendance:import")
    return render(request, "attendance/import.html", {"form": form})


# ---------------------------------------------------------------- overtime
@company_view
def overtime_list(request):
    status = request.GET.get("status", "")
    requests_ = OvertimeRequest.objects.filter(company=request.company).select_related("employee")
    if status in OvertimeRequest.Status.values:
        requests_ = requests_.filter(status=status)
    return render(request, "attendance/overtime.html", {
        "requests": requests_[:200], "status": status, "statuses": OvertimeRequest.Status.choices})


@company_view
def overtime_new(request):
    form = OvertimeForm(request.POST or None, company=request.company, initial={"date": date.today()})
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            req = services.add_request(d["employee"], d["date"], d["hours"], d["reason"], request.user)
        except ValueError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, f"Overtime of {req.hours.normalize():f} hour(s) recorded. It is waiting for approval.")
            return redirect("attendance:overtime_detail", req.pk)
    return render(request, "attendance/form.html", {"title": "Record overtime", "form": form,
                                                    "back_url": reverse("attendance:overtime")})


@company_view
def overtime_detail(request, pk):
    req = get_object_or_404(OvertimeRequest.objects.select_related("employee", "requested_by", "decided_by"),
                            pk=pk, company=request.company)
    setting = services.settings_for(request.company)
    estimate = None
    if req.status == OvertimeRequest.Status.PENDING:
        rate = services.hourly_rate(req.employee, req.date.year, req.date.month, setting)
        mult = services.multiplier_for(setting, req.day_type)
        estimate = {"rate": rate, "multiplier": mult, "amount": services._money(rate * req.hours * mult)}
    return render(request, "attendance/overtime_detail.html", {
        "req": req, "estimate": estimate, "paid": services.is_paid(req) if req.status == "APPROVED" else False,
        "can_decide": access.can_approve(request.user, request.company, "OVERTIME"),
        "waiting_for": access.approver_names(request.company, "OVERTIME"), "form": DecisionForm()})


@company_view
@require_POST
def overtime_decide(request, pk, action):
    req = get_object_or_404(OvertimeRequest, pk=pk, company=request.company)
    note = request.POST.get("note", "")
    try:
        services.decide(req, action, request.user, note)
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, {"approve": "Overtime approved. It will be paid in the next payroll run you generate.",
                                   "reject": "Overtime rejected.", "cancel": "Overtime cancelled."}[action])
    return redirect("attendance:overtime_detail", req.pk)


# ---------------------------------------------------------------- settings
@company_view
@admin_only
def settings_view(request):
    form = SettingForm(request.POST or None, instance=services.settings_for(request.company))
    if request.method == "POST" and form.is_valid():
        setting = form.save(commit=False)
        setting.company = request.company
        setting.save()
        messages.success(request, "Overtime rules saved. They apply to requests approved from now on.")
        return redirect("attendance:overtime")
    return render(request, "attendance/form.html", {
        "title": "Overtime rules", "form": form, "back_url": reverse("attendance:overtime"),
        "intro": "These figures are starting points. Confirm them against your contracts and policy. They are "
                 "applied when a request is approved, and approved requests keep the figures they were approved at."})
