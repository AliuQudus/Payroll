import csv
from datetime import date, timedelta
from functools import wraps

from django.apps import apps
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError, Q, Sum
from django.forms import modelform_factory
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from django.contrib.auth import get_user_model

from . import access, integrations, services
from .engine import FormulaError
from .forms import ApproverForm, CompanyForm, ElementForm, RunForm, UserForm, adjustment_form
from .models import (Approver, Company, Employee, EmployeeAdjustment, Payslip, PayrollRun, SalaryElement, UserPreference,
                     StatutoryRule, TaxBand)

# section -> (model, title, list columns, form fields, hint)
SECTIONS = {
    "employees": (Employee, "Employees",
                  ["employee_no", "first_name", "last_name", "department", "gross_monthly", "is_active"],
                  ["employee_no", "first_name", "last_name", "email", "department", "bank_name",
                   "account_number", "gross_monthly", "annual_rent", "pension_applies", "is_active"],
                  "Employees included in each payroll run. Per-employee formula overrides are set in the admin site."),
    "elements": (SalaryElement, "Salary elements",
                 ["code", "name", "kind", "formula", "is_taxable", "is_pensionable", ("in_leave_base", "Leave allowance base"),
                 "monthly_cap", "is_active"],
                 ElementForm.Meta.fields,
                 "Each element is a formula. Use GROSS or another element's code, for example GROSS*0.4 or MIN(BASIC*0.2, 500000)."),
    "bands": (TaxBand, "PAYE bands",
              ["effective_from", "lower_limit", "upper_limit", "rate"],
              ["effective_from", "lower_limit", "upper_limit", "rate"],
              "Annual taxable income bands. Enter the rate as a percentage, so 15 means 15%. Leave the upper limit blank for the top band."),
    "rules": (StatutoryRule, "Statutory rules",
              ["code", ("shown_value", "Value"), "effective_from"], ["code", "value", "effective_from"],
              "Enter rates as percentages, so 8 means 8%. The rent relief cap is an annual naira amount. LEAVE_ALLOWANCE is the percentage of annual pay (elements ticked Leave allowance base) paid when approved leave earns it. The latest rule effective on the pay period applies."),
}


_PERIOD = ("period", "Starts")
SECTIONS.update({
    "loans": (EmployeeAdjustment, "Loans",
              ["employee", "description", ("amount", "Loan amount"), ("progress", "Installments paid"),
               ("per_installment", "Per installment"), ("applied", "Deducted so far"), ("remaining", "Remaining"),
               ("period", "First deduction"), ("status", "Status")], [],
              "A loan is split into equal monthly installments. Deducted and remaining amounts count once a payroll run is approved; a fully repaid loan becomes Treated and is locked."),
    "absences": (EmployeeAdjustment, "Absence",
                 ["employee", "description", ("days", "Days absent"), ("period", "Month"),
                  ("applied", "Pay reduction applied"), ("status", "Status")], [],
                 "Pay for the month is reduced by gross pay divided by the absence day rule (30 by default), times the days absent."),
    "ious": (EmployeeAdjustment, "IOU",
             ["employee", "description", ("amount", "Total owed"), ("progress", "Installments paid"),
              ("per_installment", "Per installment"), ("applied", "Deducted so far"), ("remaining", "Remaining"),
              ("period", "First deduction"), ("status", "Status")], [],
             "An amount the employee owes, recovered in equal parts over the number of months you choose."),
    "additions": (EmployeeAdjustment, "Other additions",
                  ["employee", "description", ("per_installment", "Per month"), ("months", "Months"),
                   ("progress", "Months paid"), ("applied", "Paid so far"), ("remaining", "Remaining"),
                   ("period", "First payment"), "is_taxable", ("status", "Status")], [],
                  "Extra pay such as a bonus or one-off allowance. It is taxable unless you untick Taxable."),
    "deductions": (EmployeeAdjustment, "Other deductions",
                   ["employee", "description", ("per_installment", "Per month"), ("months", "Months"),
                    ("progress", "Months deducted"), ("applied", "Deducted so far"), ("remaining", "Remaining"),
                    ("period", "First deduction"), ("status", "Status")], [],
                   "Any other amount taken from net pay, for the number of months you choose."),
})
EXTRA = {"loans": {"kind": "LOAN"}, "absences": {"kind": "ABSENCE"}, "ious": {"kind": "IOU"},
         "additions": {"kind": "ADDITION"}, "deductions": {"kind": "DEDUCTION"}}


COMPANY_FORMS = {"elements": ElementForm}

HELP_TEXTS = {
    "rules": {"value": "Percentages are whole numbers: 8 means 8%. The rent relief cap is a naira amount and the absence rule is a number of days."},
    "bands": {"rate": "Percentage: 15 means 15%."},
}


def _head(model, col):
    if isinstance(col, tuple):
        return col[1]
    return model._meta.get_field(col).verbose_name.capitalize()


def _val(obj, col):
    return getattr(obj, col[0] if isinstance(col, tuple) else col)


@login_required
def home(request):
    """Staff go to the employee list. A login that belongs only to an employee goes to their own portal."""
    if request.company is None and not request.user.is_superuser:
        target = integrations.portal_home(request.user)
        if target:
            return redirect(target)
    return redirect("section_list", section="employees")


def company_view(view):
    @login_required
    @wraps(view)
    def inner(request, *args, **kwargs):
        if request.company is None:
            if request.user.is_superuser:
                messages.info(request, "Create a company to get started.")
                return redirect("company_new")
            return render(request, "payroll/no_access.html", status=403)
        return view(request, *args, **kwargs)
    return inner


def admin_only(view):
    """Administrators only: users, approvers and company settings."""
    @wraps(view)
    def inner(request, *args, **kwargs):
        if not request.user.is_superuser:
            messages.error(request, "Only administrators can do that.")
            return redirect("home")
        return view(request, *args, **kwargs)
    return inner


def _section(name):
    if name not in SECTIONS:
        raise Http404
    return SECTIONS[name]


@company_view
def section_list(request, section):
    model, title, cols, _, hint = _section(section)
    qs = model.objects.filter(company=request.company, **EXTRA.get(section, {}))
    if section in EXTRA:
        qs = qs.select_related("employee")
    status = request.GET.get("status", "open")
    if section in EXTRA and status in ("open", "treated"):
        qs = qs.filter(is_treated=(status == "treated"))
    filters = [("open", "Open"), ("treated", "Treated"), ("all", "All")] if section in EXTRA else None
    rows = [(o, [_val(o, c) for c in cols]) for o in qs]
    heads = [_head(model, c) for c in cols]
    return render(request, "payroll/list.html",
                  {"section": section, "title": title, "heads": heads, "rows": rows, "hint": hint,
                   "filters": filters, "status": status})


@company_view
def section_edit(request, section, pk=None):
    model, title, _, fields, _ = _section(section)
    extra = EXTRA.get(section, {})
    obj = get_object_or_404(model, pk=pk, company=request.company, **extra) if pk else None
    if obj is not None and getattr(obj, "is_treated", False):
        messages.info(request, "This record is treated and locked. You can view it but not edit it.")
        return redirect("section_view", section=section, pk=obj.pk)
    if section in COMPANY_FORMS:
        form = COMPANY_FORMS[section](request.POST or None, instance=obj, company=request.company)
    elif extra:
        form = adjustment_form(extra["kind"])(request.POST or None, instance=obj or model(**extra),
                                              company=request.company)
    else:
        form = modelform_factory(model, fields=fields, help_texts=HELP_TEXTS.get(section, {}))(
            request.POST or None, instance=obj)
    if section == "employees":
        integrations.lock_gross(form, obj)
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
            return redirect("section_list", section=section)
    return render(request, "payroll/form.html",
                  {"form": form, "title": ("Edit " if obj else "Add to ") + title.lower(), "back": section})


@company_view
def section_delete(request, section, pk):
    model, title, *_ = _section(section)
    obj = get_object_or_404(model, pk=pk, company=request.company, **EXTRA.get(section, {}))
    if getattr(obj, "is_treated", False):
        messages.info(request, "This record is treated and locked, so it cannot be deleted. You can still view it.")
        return redirect("section_view", section=section, pk=obj.pk)
    if request.method == "POST":
        try:
            obj.delete()
            messages.success(request, "Deleted.")
        except ProtectedError:
            messages.error(request, "This record is used by payslips. Mark it inactive instead.")
        return redirect("section_list", section=section)
    return render(request, "payroll/confirm.html", {"obj": obj, "back": section})


@company_view
def run_list(request):
    form = RunForm(request.POST or None,
                   initial={"year": date.today().year, "month": date.today().month})
    if request.method == "POST" and form.is_valid():
        run = form.save(commit=False)
        run.company, run.created_by = request.company, request.user
        try:
            with transaction.atomic():
                run.save()
        except IntegrityError:
            messages.error(request, "A run for that month already exists.")
        else:
            return redirect("run_detail", pk=run.pk)
    runs = PayrollRun.objects.filter(company=request.company).order_by("-year", "-month")
    return render(request, "payroll/runs.html", {"form": form, "runs": runs})


ACTIONS = {
    "DRAFT": [("generate", "Generate payslips"), ("submit", "Submit for approval"), ("delete", "Delete run")],
    "PENDING": [("approve", "Approve"), ("reopen", "Send back to draft")],
    "APPROVED": [("pay", "Mark as paid")],
    "PAID": [],
}


@company_view
def run_detail(request, pk):
    run = get_object_or_404(PayrollRun, pk=pk, company=request.company)
    slips = run.payslips.select_related("employee").order_by("employee__employee_no")
    totals = slips.aggregate(gross=Sum("gross"), paye=Sum("paye"), net=Sum("net_pay"), cost=Sum("employer_cost"))
    can = access.can_approve(request.user, run.company, "PAYROLL")
    return render(request, "payroll/run_detail.html",
                  {"run": run, "slips": slips, "totals": totals, "final": run.status in ("APPROVED", "PAID"),
                   "actions": [a for a in ACTIONS[run.status] if can or a[0] != "approve"],
                   "cannot_approve": run.status == "PENDING" and not can,
                   "waiting_for": access.approver_names(run.company, "PAYROLL"),
                   "employee_count": Employee.objects.filter(company=request.company, is_active=True).count()})


def _report_email(request, r):
    if r["sent"]:
        messages.success(request, f"Emailed {r['sent']} payslip(s).")
    elif not r["skipped"] and not r["failed"]:
        messages.info(request, "Every payslip has already been emailed. Use Resend to everyone to send them again.")
    if r["skipped"]:
        messages.warning(request, "No email address for: " + ", ".join(r["skipped"]) + ".")
    for problem in r["failed"][:5]:
        messages.error(request, "Not sent - " + problem)


@company_view
@require_POST
def run_action(request, pk, action):
    run = get_object_or_404(PayrollRun, pk=pk, company=request.company)
    try:
        result = services.perform(run, action, request.user)
    except (FormulaError, ValueError) as exc:
        messages.error(request, str(exc))
    else:
        if action == "generate":
            messages.success(request, f"Generated {result} payslip(s).")
            negative = run.payslips.filter(net_pay__lt=0).select_related("employee")
            if negative:
                names = ", ".join(f"{p.employee.first_name} {p.employee.last_name}" for p in negative)
                messages.warning(request, f"Negative net pay for: {names}. Check your salary elements and statutory rules.")
            unpaid = integrations.unpaid_allowances(run)
            if unpaid:
                names = ", ".join(f"{e.first_name} {e.last_name}" for e in unpaid)
                messages.warning(request, f"Leave allowance not paid for: {names}. Add a LEAVE_ALLOWANCE statutory rule and "
                                          "tick 'Leave allowance base' on the salary elements it is worked out from.")
        else:
            messages.success(request, "Done.")
        if action == "delete":
            return redirect("run_list")
    return redirect("run_detail", pk=pk)


@company_view
def run_csv(request, pk):
    run = get_object_or_404(PayrollRun, pk=pk, company=request.company)
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="payroll-{run.year}-{run.month:02d}.csv"'
    writer = csv.writer(response)
    writer.writerow(["Employee no", "Name", "Bank", "Account number", "Net pay"])
    for p in run.payslips.select_related("employee").order_by("employee__employee_no"):
        e = p.employee
        writer.writerow([e.employee_no, f"{e.first_name} {e.last_name}", e.bank_name, e.account_number, p.net_pay])
    return response


@company_view
def payslip_detail(request, pk):
    slip = get_object_or_404(Payslip.objects.select_related("employee", "run"), pk=pk, run__company=request.company)
    return render(request, "payroll/payslip.html", {"slip": slip, "groups": services.payslip_groups(slip)})


@company_view
def calculator(request):
    return render(request, "payroll/calculator.html")


@login_required
@admin_only
def company_new(request):
    form = CompanyForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        company = form.save()
        request.session["company_id"] = company.pk
        return redirect("home")
    return render(request, "payroll/form.html", {"form": form, "title": "New company", "back": "employees"})


@login_required
@require_POST
def company_switch(request):
    company = access.companies_for(request.user).filter(pk=request.POST.get("company")).first()
    if company:
        request.session["company_id"] = company.pk
    return redirect(request.META.get("HTTP_REFERER") or "home")


@company_view
@admin_only
def company_edit(request):
    form = CompanyForm(request.POST or None, instance=request.company)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Company settings saved.")
        return redirect("home")
    return render(request, "payroll/form.html",
                  {"form": form, "title": "Company settings", "back": "employees", "test_email": True})


@company_view
@admin_only
@require_POST
def company_test_email(request):
    try:
        services.send_test_email(request.company)
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Test email sent to {request.company.email}. Check that inbox, and the spam folder.")
    return redirect("company_edit")


# ---------------------------------------------------------------- payslips module
@company_view
def payslip_list(request):
    runs = PayrollRun.objects.filter(company=request.company, status__in=["APPROVED", "PAID"]).order_by("-year", "-month")
    run = runs.filter(pk=request.GET["run"]).first() if request.GET.get("run", "").isdigit() else None
    run = run or runs.first()
    q = request.GET.get("q", "").strip()
    slips = []
    if run:
        slips = run.payslips.select_related("employee").order_by("employee__employee_no")
        if q:
            slips = slips.filter(Q(employee__first_name__icontains=q) | Q(employee__last_name__icontains=q)
                                 | Q(employee__employee_no__icontains=q))
    return render(request, "payroll/payslip_list.html", {"runs": runs, "run": run, "slips": slips, "q": q})


@company_view
@require_POST
def payslip_email(request):
    run = get_object_or_404(PayrollRun, pk=request.POST.get("run"), company=request.company)
    mode = request.POST.get("mode", "unsent")
    only = [int(request.POST["payslip"])] if mode == "one" and request.POST.get("payslip", "").isdigit() else None
    try:
        result = services.send_payslips(run, resend=(mode == "all"), only_ids=only)
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        _report_email(request, result)
    return redirect(f"{reverse('payslip_list')}?run={run.pk}")


@company_view
def section_view(request, section, pk):
    """Read-only page, used for treated (locked) records."""
    model, title, cols, _, _ = _section(section)
    obj = get_object_or_404(model, pk=pk, company=request.company, **EXTRA.get(section, {}))
    rows = [(_head(model, c), _val(obj, c)) for c in cols]
    return render(request, "payroll/detail.html", {
        "section": section, "obj": obj, "rows": rows, "title": f"{title}: {obj}",
        "locked": getattr(obj, "is_treated", False)})


# ---------------------------------------------------------------- users and approvers (administrators)
@login_required
@admin_only
def user_list(request):
    users = get_user_model().objects.order_by("username").prefetch_related("company_access__company", "approver_roles__company")
    rows = []
    for u in users:
        companies = "All companies" if u.is_superuser else (", ".join(a.company.name for a in u.company_access.all()) or "None")
        roles = ", ".join(f"{r.get_scope_display()} ({r.company.name})" for r in u.approver_roles.all()) or "-"
        rows.append((u, companies, roles))
    return render(request, "payroll/users.html", {"rows": rows})


@login_required
@admin_only
def user_edit(request, pk=None):
    user = get_object_or_404(get_user_model(), pk=pk) if pk else None
    form = UserForm(request.POST or None, instance=user, acting_user=request.user)
    if request.method == "POST" and form.is_valid():
        saved = form.save()
        messages.success(request, f"User {saved.username} saved.")
        return redirect("user_list")
    return render(request, "payroll/user_form.html", {"form": form, "obj": user})


@company_view
@admin_only
def approver_list(request):
    form = ApproverForm(request.POST or None, company=request.company)
    if request.method == "POST" and form.is_valid():
        item = form.save(commit=False)
        item.company = request.company
        try:
            with transaction.atomic():
                item.save()
        except IntegrityError:
            messages.error(request, "That user is already an approver for this.")
        else:
            messages.success(request, "Approver added.")
            return redirect("approver_list")
    rows = Approver.objects.filter(company=request.company).select_related("user")
    summary = [(label, access.approver_names(request.company, scope)) for scope, label in Approver.Scope.choices]
    return render(request, "payroll/approvers.html", {"form": form, "rows": rows, "summary": summary})


@company_view
@admin_only
@require_POST
def approver_delete(request, pk):
    get_object_or_404(Approver, pk=pk, company=request.company).delete()
    messages.success(request, "Approver removed.")
    return redirect("approver_list")


# ---------------------------------------------------------------- appearance and the account page
def layout_template(request):
    """Staff get the payroll layout; someone who only has an employee-portal login gets the portal layout."""
    employee_only = (not request.user.is_superuser and getattr(request, "company", None) is None
                     and hasattr(request.user, "employee_account"))
    return "portal/base.html" if employee_only else "payroll/base.html"


def _save_theme(user, theme):
    UserPreference.objects.update_or_create(user=user, defaults={"theme": theme})


@login_required
@require_POST
def set_theme(request):
    """Save the light/dark choice. The page's own script calls this in the background; without a script, it is an
    ordinary form post that comes back to the same page."""
    theme = request.POST.get("theme")
    quiet = request.headers.get("X-Requested-With") == "fetch"
    if theme not in UserPreference.Theme.values:
        if quiet:
            return HttpResponse(status=400)
        messages.error(request, "That is not a choice of appearance.")
    else:
        _save_theme(request.user, theme)
        if quiet:
            return HttpResponse(status=204)
    target = request.POST.get("next") or "/"
    ok = url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}, require_https=request.is_secure())
    return redirect(target if ok else "/")


@login_required
def account(request):
    user = request.user
    if request.method == "POST":
        theme = request.POST.get("theme")
        if theme in UserPreference.Theme.values:
            _save_theme(user, theme)
            messages.success(request, "Your appearance is saved.")
            return redirect("account")
        messages.error(request, "Choose one of the options.")
    ctx = {"base_template": layout_template(request), "themes": UserPreference.Theme.choices,
           "current_theme": getattr(getattr(user, "preference", None), "theme", "system")}
    if apps.is_installed("security"):
        from security import twofactor
        ctx.update(security_installed=True, two_factor_on=twofactor.is_enabled(user),
                   two_factor_required=twofactor.policy_requires(user))
    return render(request, "payroll/account.html", ctx)
