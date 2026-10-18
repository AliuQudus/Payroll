import os

from django.contrib import messages
from django.db.models import Q
from django.http import FileResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from payroll.models import Employee
from payroll.views import company_view

from . import services
from .forms import ContactForm, DocumentForm, ProfileForm
from .models import EmergencyContact, EmployeeDocument, EmployeeProfile


def _employee(request, pk):
    return get_object_or_404(Employee, pk=pk, company=request.company)


def _detail_url(employee):
    return reverse("hr:detail", args=[employee.pk])


@company_view
def directory(request):
    q = request.GET.get("q", "").strip()
    show = "all" if request.GET.get("show") == "all" else "active"
    people = Employee.objects.filter(company=request.company)
    if show == "active":
        people = people.filter(is_active=True)
    if q:
        people = people.filter(Q(first_name__icontains=q) | Q(last_name__icontains=q)
                               | Q(employee_no__icontains=q) | Q(department__icontains=q)
                               | Q(profile__job_title__icontains=q))
    profiles = {p.employee_id: p for p in
                EmployeeProfile.objects.filter(employee__company=request.company).select_related("manager")}
    rows = []
    for e in people.order_by("employee_no"):
        p = profiles.get(e.pk)
        rows.append({"emp": e, "profile": p, "service": services.service_text(p.hire_date) if p else ""})
    return render(request, "hr/directory.html", {"rows": rows, "q": q, "show": show})


@company_view
def detail(request, pk):
    emp = _employee(request, pk)
    profile = EmployeeProfile.objects.filter(employee=emp).select_related("manager").first()
    return render(request, "hr/detail.html", {
        "emp": emp, "profile": profile,
        "service": services.service_text(profile.hire_date) if profile else "",
        "contacts": emp.emergency_contacts.all(),
        "documents": emp.documents.all(),
        "reports": Employee.objects.filter(company=request.company, is_active=True, profile__manager=emp)
                           .order_by("employee_no"),
    })


@company_view
def profile_edit(request, pk):
    emp = _employee(request, pk)
    profile = EmployeeProfile.objects.filter(employee=emp).first() or EmployeeProfile(employee=emp)
    form = ProfileForm(request.POST or None, instance=profile, company=request.company)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f"HR record saved for {emp}.")
        return redirect(_detail_url(emp))
    return render(request, "hr/form.html", {"title": f"HR record: {emp}", "form": form,
                                            "back_url": _detail_url(emp)})


@company_view
def contact_edit(request, employee_pk=None, pk=None):
    if pk:
        contact = get_object_or_404(EmergencyContact, pk=pk, employee__company=request.company)
        emp = contact.employee
    else:
        emp = _employee(request, employee_pk)
        contact = EmergencyContact(employee=emp)
    form = ContactForm(request.POST or None, instance=contact)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Contact saved.")
        return redirect(_detail_url(emp))
    return render(request, "hr/form.html", {"title": f"{'Edit' if pk else 'Add'} contact for {emp}",
                                            "form": form, "back_url": _detail_url(emp)})


@company_view
@require_POST
def contact_delete(request, pk):
    contact = get_object_or_404(EmergencyContact, pk=pk, employee__company=request.company)
    contact.delete()
    messages.success(request, "Contact removed.")
    return redirect(_detail_url(contact.employee))


@company_view
def document_new(request, employee_pk):
    emp = _employee(request, employee_pk)
    form = DocumentForm(request.POST or None, request.FILES or None, instance=EmployeeDocument(employee=emp))
    if request.method == "POST" and form.is_valid():
        doc = form.save(commit=False)
        doc.uploaded_by = request.user
        doc.save()
        messages.success(request, f"{doc.title} uploaded.")
        return redirect(_detail_url(emp))
    return render(request, "hr/form.html", {"title": f"Upload a document for {emp}", "form": form,
                                            "back_url": _detail_url(emp), "multipart": True})


@company_view
def document_download(request, pk):
    doc = get_object_or_404(EmployeeDocument, pk=pk, employee__company=request.company)
    try:
        handle = doc.file.open("rb")
    except (FileNotFoundError, ValueError):
        messages.error(request, "That file is missing from storage. Upload it again.")
        return redirect(_detail_url(doc.employee))
    # always a download, never rendered inside the site
    return FileResponse(handle, as_attachment=True, filename=doc.original_name or os.path.basename(doc.file.name))


@company_view
@require_POST
def document_delete(request, pk):
    doc = get_object_or_404(EmployeeDocument, pk=pk, employee__company=request.company)
    emp = doc.employee
    doc.delete()
    messages.success(request, "Document deleted.")
    return redirect(_detail_url(emp))


@company_view
def alerts(request):
    try:
        days = int(request.GET.get("days", 30))
    except ValueError:
        days = 30
    days = days if days in (30, 60, 90) else 30
    data = services.alerts(request.company, days=days)
    needs_action = sum(len(data[k]) for k in ("documents", "contracts", "probation", "missing"))
    return render(request, "hr/alerts.html", {**data, "days": days, "needs_action": needs_action})
