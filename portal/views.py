from datetime import date, timedelta
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from payroll import services as payroll_services
from payroll.models import Employee
from payroll.views import admin_only, company_view

from . import services


# ---------------------------------------------------------------- the employee's own pages
def portal_view(view):
    """Only a login linked to a current employee gets in, and the view only ever sees that employee."""
    @login_required
    @wraps(view)
    def inner(request, *args, **kwargs):
        account = getattr(request.user, "employee_account", None)
        if account is None or not account.employee.is_active:
            reason = "left" if account is not None else "none"
            return render(request, "portal/no_portal.html", {"reason": reason}, status=403)
        request.employee = account.employee
        request.portal_leave, request.portal_hr = services.has("leave"), services.has("hr")
        request.portal_attendance = services.has("attendance")
        request.portal_staff = request.company is not None      # also works in the payroll screens
        return view(request, *args, **kwargs)
    return inner


def _need(app_name):
    if not services.has(app_name):
        raise Http404


@portal_view
def home(request):
    emp = request.employee
    ctx = {"latest": services.visible_payslips(emp).first()}
    if request.portal_leave:
        from . import leave_support
        ctx["balances"] = [(lt, b) for lt, b in leave_support.balances(emp, date.today().year) if b["entitled"]]
        ctx["pending"] = leave_support.requests_of(emp).filter(status="PENDING").count()
    if request.portal_attendance:
        from . import attendance_support
        ctx["absent_days"], ctx["overtime_waiting"] = attendance_support.home_figures(emp)
    return render(request, "portal/home.html", ctx)


@portal_view
def payslips(request):
    return render(request, "portal/payslips.html", {"slips": services.visible_payslips(request.employee)})


@portal_view
def payslip(request, pk):
    slip = get_object_or_404(services.visible_payslips(request.employee), pk=pk)      # own and approved only
    return render(request, "portal/payslip.html", {"slip": slip, "groups": payroll_services.payslip_groups(slip)})


# ---- leave
@portal_view
def leave(request):
    _need("leave")
    from . import leave_support
    emp = request.employee
    return render(request, "portal/leave.html", {
        "balances": leave_support.balances(emp, date.today().year), "requests": leave_support.requests_of(emp),
        "year": date.today().year})


@portal_view
def leave_new(request):
    _need("leave")
    from . import leave_support
    from leave.models import LeaveRequest
    emp = request.employee
    form = leave_support.LeaveForm(request.POST or None, instance=LeaveRequest(company=emp.company, employee=emp),
                                   employee=emp)
    if request.method == "POST" and form.is_valid():
        req = form.save(commit=False)
        req.requested_by, req.days = request.user, form.days
        req.save()
        messages.success(request, f"Your request for {req.days.normalize():f} day(s) has been sent for approval.")
        return redirect("portal:leave")
    return render(request, "portal/form.html", {"title": "Request leave", "form": form,
                                                "back_url": reverse("portal:leave")})


@portal_view
@require_POST
def leave_cancel(request, pk):
    _need("leave")
    from leave import services as leave_services
    from leave.models import LeaveRequest
    req = get_object_or_404(LeaveRequest, pk=pk, employee=request.employee)
    if req.status != "PENDING":
        messages.error(request, "Only a request that is still waiting for approval can be withdrawn here. "
                                "Ask HR to cancel an approved one.")
    else:
        leave_services.decide(req, "cancel", request.user)
        messages.success(request, "Your request has been withdrawn.")
    return redirect("portal:leave")


# ---- attendance and overtime
def _month(text):
    try:
        year, month = (int(x) for x in (text or "").split("-"))
        return date(year, month, 1)
    except ValueError:
        return date.today().replace(day=1)


@portal_view
def attendance(request):
    _need("attendance")
    from . import attendance_support as support
    first = _month(request.GET.get("month"))
    records, counts, hours, amount = support.month_of(request.employee, first)
    nxt = (first + timedelta(days=32)).replace(day=1)
    return render(request, "portal/attendance.html", {
        "first": first, "records": records, "counts": counts, "ot_hours": hours, "ot_amount": amount,
        "prev": (first - timedelta(days=1)).replace(day=1), "next": nxt if nxt <= date.today() else None})


@portal_view
def overtime(request):
    _need("attendance")
    from . import attendance_support as support
    requests_ = list(support.my_requests(request.employee)[:100])
    return render(request, "portal/overtime.html", {"requests": requests_, "paid": support.paid_ids(requests_)})


@portal_view
def overtime_new(request):
    _need("attendance")
    from . import attendance_support as support
    form = support.OvertimeForm(request.POST or None, initial={"date": date.today()})
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            req = support.request_overtime(request.employee, request.user, d["date"], d["hours"], d["reason"])
        except ValueError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, f"Your overtime request for {req.hours.normalize():f} hour(s) has been sent for approval.")
            return redirect("portal:overtime")
    return render(request, "portal/form.html", {"title": "Request overtime", "form": form,
                                                "back_url": reverse("portal:overtime")})


@portal_view
@require_POST
def overtime_cancel(request, pk):
    _need("attendance")
    from . import attendance_support as support
    from attendance.models import OvertimeRequest
    req = get_object_or_404(OvertimeRequest, pk=pk, employee=request.employee)
    if req.status != OvertimeRequest.Status.PENDING:
        messages.error(request, "Only a request that is still waiting for approval can be withdrawn here. "
                                "Ask HR about an approved one.")
    else:
        support.withdraw(req, request.user)
        messages.success(request, "Your overtime request has been withdrawn.")
    return redirect("portal:overtime")


# ---- personal details
@portal_view
def details(request):
    _need("hr")
    from . import hr_support
    emp = request.employee
    return render(request, "portal/details.html", {
        "profile": hr_support.profile_of(emp), "contacts": hr_support.contacts_of(emp),
        "account_masked": services.mask_account(emp.account_number)})


@portal_view
def details_edit(request):
    _need("hr")
    from . import hr_support
    profile = hr_support.profile_of(request.employee)
    if profile is None:
        messages.error(request, "HR has not set up your record yet.")
        return redirect("portal:details")
    form = hr_support.DetailsForm(request.POST or None, instance=profile)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Your details have been updated.")
        return redirect("portal:details")
    return render(request, "portal/form.html", {"title": "Update your phone and address", "form": form,
                                                "back_url": reverse("portal:details")})


@portal_view
def contact_edit(request, pk=None):
    _need("hr")
    from . import hr_support
    emp = request.employee
    contact = (get_object_or_404(hr_support.contacts_of(emp), pk=pk) if pk
               else hr_support.EmergencyContact(employee=emp))
    form = hr_support.ContactForm(request.POST or None, instance=contact)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Contact saved.")
        return redirect("portal:details")
    return render(request, "portal/form.html", {"title": "Edit contact" if pk else "Add an emergency contact",
                                                "form": form, "back_url": reverse("portal:details")})


@portal_view
@require_POST
def contact_delete(request, pk):
    _need("hr")
    from . import hr_support
    get_object_or_404(hr_support.contacts_of(request.employee), pk=pk).delete()
    messages.success(request, "Contact removed.")
    return redirect("portal:details")


# ---------------------------------------------------------------- administrators: who has a login
LABELS = {"none": "No access", "invited": "Invited, not signed in yet", "active": "Active",
          "revoked": "Access switched off", "left": "Has left"}


@company_view
@admin_only
def access(request):
    people = (Employee.objects.filter(company=request.company).select_related("portal_account__user")
              .order_by("employee_no"))
    rows = []
    for emp in people:
        status = services.status_of(emp)
        if not emp.is_active and status == "none":
            continue                                    # people who left and never had a login are not listed
        account = getattr(emp, "portal_account", None)
        user = account.user if account else None
        rows.append({"emp": emp, "status": status, "label": LABELS[status], "account": account,
                     "also_staff": bool(user and (user.is_superuser or user.company_access.exists())),
                     "last_login": user.last_login if user else None})
    return render(request, "portal/access.html", {"rows": rows})


def _employee(request, pk):
    return get_object_or_404(Employee, pk=pk, company=request.company)


def _report_invite(request, account, problem, link):
    if problem is None:
        messages.success(request, f"An invitation has been emailed to {account.employee.email}.")
    else:
        messages.warning(request, f"The invitation email was not sent ({problem}). Give {account.employee} this "
                                  f"link yourself. It works once: {link}")


@company_view
@admin_only
@require_POST
def access_grant(request, pk):
    emp = _employee(request, pk)
    try:
        account = services.grant(emp, request.user)
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        _report_invite(request, account, *services.send_invite(account, request))
    return redirect("portal:access")


@company_view
@admin_only
@require_POST
def access_invite(request, pk):
    emp = _employee(request, pk)
    account = getattr(emp, "portal_account", None)
    if account is None or not emp.is_active:
        messages.error(request, "This employee has no portal login to invite.")
    else:
        if not account.user.is_active:
            services.restore(account)
        _report_invite(request, account, *services.send_invite(account, request))
    return redirect("portal:access")


@company_view
@admin_only
@require_POST
def access_revoke(request, pk):
    emp = _employee(request, pk)
    account = getattr(emp, "portal_account", None)
    if account is None:
        messages.error(request, "This employee has no portal login.")
    else:
        services.revoke(account)
        messages.success(request, f"{emp} can no longer sign in.")
    return redirect("portal:access")
