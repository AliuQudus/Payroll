from datetime import date, timedelta

from django.contrib import messages
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.forms import modelform_factory
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from payroll import access
from payroll.views import _head, _val, company_view

from . import services
from .forms import EntitlementForm, LeaveRequestForm
from .models import LeaveEntitlement, LeaveRequest, LeaveType, PublicHoliday

# section -> (model, title, list columns, form fields, hint)
SECTIONS = {
    "types": (LeaveType, "Leave types",
              ["name", ("days_per_year", "Days per year"), "is_paid", "working_days_only", "earns_allowance", "is_active"],
              ["name", "days_per_year", "is_paid", "working_days_only", "earns_allowance", "is_active"],
              "Declare each kind of leave and how many days a year it allows (0 means no limit). Unpaid types reduce pay; types that earn the leave allowance make an employee eligible once approved."),
    "holidays": (PublicHoliday, "Public holidays", ["date", "name"], ["date", "name"],
                 "These dates are skipped when counting the working days of a leave request."),
    "entitlements": (LeaveEntitlement, "Leave entitlements", ["employee", "leave_type", "year", "days"], [],
                     "Give one employee a different number of days from the leave type's default for a year, for example to carry days over."),
}
COMPANY_FORMS = {"entitlements": EntitlementForm}


def _section(name):
    if name not in SECTIONS:
        raise Http404
    return SECTIONS[name]


@company_view
def section_list(request, section):
    model, title, cols, _, hint = _section(section)
    qs = model.objects.filter(company=request.company)
    if section == "entitlements":
        qs = qs.select_related("employee", "leave_type")
    rows = [(o, [_val(o, c) for c in cols]) for o in qs]
    return render(request, "leave/section_list.html", {
        "section": section, "title": title, "heads": [_head(model, c) for c in cols], "rows": rows, "hint": hint})


@company_view
def section_edit(request, section, pk=None):
    model, title, _, fields, _ = _section(section)
    obj = get_object_or_404(model, pk=pk, company=request.company) if pk else None
    if section in COMPANY_FORMS:
        form = COMPANY_FORMS[section](request.POST or None, instance=obj, company=request.company)
    else:
        form = modelform_factory(model, fields=fields)(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        item = form.save(commit=False)
        item.company = request.company
        try:
            with transaction.atomic():
                item.save()
        except IntegrityError:
            form.add_error(None, "A record with these details already exists.")
        else:
            messages.success(request, "Saved.")
            return redirect("leave:section_list", section=section)
    return render(request, "payroll/form.html", {
        "form": form, "title": ("Edit " if obj else "Add to ") + title.lower(),
        "back_url": reverse("leave:section_list", args=[section])})


@company_view
def section_delete(request, section, pk):
    model, title, *_ = _section(section)
    obj = get_object_or_404(model, pk=pk, company=request.company)
    if request.method == "POST":
        try:
            obj.delete()
            messages.success(request, "Deleted.")
        except ProtectedError:
            messages.error(request, "This record is used by leave requests. Mark it inactive instead.")
        return redirect("leave:section_list", section=section)
    return render(request, "leave/confirm.html", {"obj": obj, "back": section})


@company_view
def requests(request):
    status = request.GET.get("status", "pending")
    today = date.today()
    base = LeaveRequest.objects.filter(company=request.company).select_related("employee", "leave_type")
    qs = base.filter(status=status.upper()) if status in ("pending", "approved", "rejected", "cancelled") else base
    approved = base.filter(status="APPROVED")
    return render(request, "leave/requests.html", {
        "requests": qs, "status": status,
        "filters": [("pending", "Pending"), ("approved", "Approved"), ("rejected", "Rejected"),
                    ("cancelled", "Cancelled"), ("all", "All")],
        "on_leave": approved.filter(start_date__lte=today, end_date__gte=today),
        "upcoming": approved.filter(start_date__gt=today, start_date__lte=today + timedelta(days=30)).order_by("start_date"),
    })


@company_view
def new(request):
    form = LeaveRequestForm(request.POST or None, company=request.company)
    if request.method == "POST" and form.is_valid():
        req = form.save(commit=False)
        req.company, req.requested_by, req.days = request.company, request.user, form.days
        req.save()
        messages.success(request, f"Leave request saved for {req.days.normalize():f} day(s). It is waiting for approval.")
        return redirect("leave:requests")
    return render(request, "payroll/form.html",
                  {"form": form, "title": "New leave request", "back_url": reverse("leave:requests")})


@company_view
def detail(request, pk):
    req = get_object_or_404(LeaveRequest.objects.select_related(
        "employee", "leave_type", "relief_officer", "requested_by", "decided_by"), pk=pk, company=request.company)
    balances = [(y, services.balance(req.employee, req.leave_type, y))
                for y in range(req.start_date.year, req.end_date.year + 1)]
    can_decide = access.can_approve(request.user, req.company, "LEAVE")
    return render(request, "leave/detail.html", {
        "req": req, "balances": balances, "can_decide": can_decide,
        "waiting_for": access.approver_names(req.company, "LEAVE")})


@company_view
@require_POST
def action(request, pk, action):
    req = get_object_or_404(LeaveRequest, pk=pk, company=request.company)
    try:
        services.decide(req, action, request.user, request.POST.get("note", ""))
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Leave request {req.get_status_display().lower()}.")
        outcome = services.notify(req)
        if outcome is True:
            messages.info(request, f"{req.employee.first_name} has been told by email.")
        elif outcome:
            messages.warning(request, f"The decision is saved, but the email to {req.employee.first_name} "
                                      f"could not be sent: {outcome}")
    return redirect("leave:detail", pk=pk)


@company_view
def balances(request):
    try:
        year = int(request.GET.get("year", date.today().year))
    except ValueError:
        year = date.today().year
    types, rows = services.year_report(request.company, year)
    return render(request, "leave/balances.html", {"year": year, "types": types, "rows": rows})
