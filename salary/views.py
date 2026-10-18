from datetime import date

from django.contrib import messages
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from payroll.models import Employee
from payroll.views import company_view

from . import services
from .forms import GradeForm, RevisionForm
from .models import PayGrade, SalaryRevision


def _employee(request, pk):
    return get_object_or_404(Employee, pk=pk, company=request.company)


def _band_warning(revision):
    grade = revision.grade if revision else None
    if grade and not grade.contains(revision.gross_monthly):
        return f"{revision.gross_monthly:,.2f} is outside the {grade.name} band ({grade.min_gross:,.2f} to {grade.max_gross:,.2f})."
    return ""


@company_view
def overview(request):
    today = date.today()
    rows = []
    for emp in Employee.objects.filter(company=request.company, is_active=True).order_by("employee_no"):
        current = services.current_revision(emp, today)
        last = emp.salary_revisions.order_by("-effective_from").first()
        rows.append({"emp": emp, "current": current, "last": last, "count": emp.salary_revisions.count(),
                     "gross": current.gross_monthly if current else emp.gross_monthly,
                     "warning": _band_warning(current)})
    month, pending = services.pending(request.company)
    return render(request, "salary/overview.html", {"rows": rows, "pending": pending, "pending_month": month})


@company_view
def detail(request, pk):
    emp = _employee(request, pk)
    revisions = list(emp.salary_revisions.select_related("grade", "created_by").order_by("effective_from"))
    history, previous = [], None
    for rev in revisions:
        change = rev.gross_monthly - previous.gross_monthly if previous else None
        pct = (change / previous.gross_monthly * 100) if previous and previous.gross_monthly else None
        history.append({"rev": rev, "change": change, "pct": pct, "locked": services.is_locked(rev),
                        "deletable": not services.is_locked(rev) and not (
                            rev.reason == SalaryRevision.Reason.OPENING and len(revisions) > 1)})
        previous = rev
    history.reverse()
    nxt = services.first_open_month(request.company)
    owed, _, over = services.arrears_for(emp, request.company, nxt.year, nxt.month)
    current = services.current_revision(emp)
    return render(request, "salary/detail.html", {
        "emp": emp, "history": history, "next_month": nxt, "owed": owed, "overpaid": over,
        "current": current, "warning": _band_warning(current),
        "gross": current.gross_monthly if current else emp.gross_monthly})


@company_view
def revision_new(request, pk):
    emp = _employee(request, pk)
    form = RevisionForm(request.POST or None, instance=SalaryRevision(company=request.company, employee=emp),
                        employee=emp, user=request.user)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            services.add_revision(emp, d["effective_from"], d["gross_monthly"], d["reason"], d["grade"],
                                  d["note"], request.user)
        except IntegrityError:
            form.add_error("effective_from", "This employee already has a change from that month.")
        else:
            nxt = services.first_open_month(request.company)
            owed, _, over = services.arrears_for(emp, request.company, nxt.year, nxt.month)
            messages.success(request, f"Salary change saved for {emp}.")
            if owed:
                messages.warning(request, f"This change is dated in months that were already paid. Back pay of "
                                          f"₦{owed:,.2f} will be added to the {nxt:%B %Y} payroll when you generate it.")
            if over:
                messages.warning(request, f"The employee was overpaid by ₦{over:,.2f} in earlier months. "
                                          "This is not taken back automatically.")
            return redirect("salary:detail", emp.pk)
    return render(request, "salary/form.html", {"title": f"New salary change for {emp}", "form": form,
                                                "back_url": reverse("salary:detail", args=[emp.pk])})


@company_view
@require_POST
def revision_delete(request, pk):
    rev = get_object_or_404(SalaryRevision, pk=pk, company=request.company)
    emp_pk = rev.employee_id
    try:
        services.delete_revision(rev)
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Salary change deleted.")
    return redirect("salary:detail", emp_pk)


@company_view
def grades(request):
    return render(request, "salary/grades.html", {"grades": PayGrade.objects.filter(company=request.company)})


@company_view
def grade_edit(request, pk=None):
    grade = get_object_or_404(PayGrade, pk=pk, company=request.company) if pk else PayGrade(company=request.company)
    form = GradeForm(request.POST or None, instance=grade)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                form.save()
        except IntegrityError:
            form.add_error("name", "There is already a grade with that name.")
        else:
            messages.success(request, "Grade saved.")
            return redirect("salary:grades")
    return render(request, "salary/form.html", {"title": "Edit grade" if pk else "New grade", "form": form,
                                                "back_url": reverse("salary:grades")})


@company_view
@require_POST
def grade_delete(request, pk):
    grade = get_object_or_404(PayGrade, pk=pk, company=request.company)
    try:
        grade.delete()
        messages.success(request, "Grade deleted.")
    except ProtectedError:
        messages.error(request, "This grade is used in salary history, so it cannot be deleted.")
    return redirect("salary:grades")
