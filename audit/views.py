import csv
from datetime import timedelta

from django import forms
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import render

from payroll.views import admin_only, company_view

from .models import AuditEntry

EXPORT_LIMIT = 20000
PAGE_SIZE = 50


class FilterForm(forms.Form):
    q = forms.CharField(required=False, label="Search")
    user = forms.ChoiceField(required=False)
    area = forms.ChoiceField(required=False)
    action = forms.ChoiceField(required=False, choices=[("", "Any action")] + AuditEntry.Action.choices)
    date_from = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    date_to = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    employee = forms.IntegerField(required=False, widget=forms.HiddenInput)

    def __init__(self, data, scope):
        super().__init__(data)
        people = scope.exclude(username="").values_list("username", flat=True).distinct().order_by("username")
        areas = scope.exclude(model_name="").values_list("model_name", flat=True).distinct().order_by("model_name")
        self.fields["user"].choices = [("", "Anyone")] + [(u, u) for u in people]
        self.fields["area"].choices = [("", "Anything")] + [(a, a) for a in areas]


def scope_for(company):
    """This company's entries, plus the ones that belong to no company (sign-ins, users)."""
    return AuditEntry.objects.filter(Q(company_id=company.pk) | Q(company_id__isnull=True))


def filtered(company, params):
    scope = scope_for(company)
    form = FilterForm(params, scope)
    entries = scope
    if form.is_valid():
        d = form.cleaned_data
        if d["q"]:
            entries = entries.filter(Q(object_repr__icontains=d["q"]) | Q(subject__icontains=d["q"])
                                     | Q(username__icontains=d["q"]))
        if d["user"]:
            entries = entries.filter(username=d["user"])
        if d["area"]:
            entries = entries.filter(model_name=d["area"])
        if d["action"]:
            entries = entries.filter(action=d["action"])
        if d["date_from"]:
            entries = entries.filter(created_at__date__gte=d["date_from"])
        if d["date_to"]:
            entries = entries.filter(created_at__date__lte=d["date_to"])
        if d["employee"]:
            entries = entries.filter(employee_id=d["employee"])
    return form, entries.select_related("user")


@company_view
@admin_only
def log(request):
    form, entries = filtered(request.company, request.GET)
    page = Paginator(entries, PAGE_SIZE).get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    return render(request, "audit/log.html", {"form": form, "page": page, "querystring": params.urlencode(),
                                              "export_limit": EXPORT_LIMIT})


def _cell(value):
    """Stop text that starts with = + - or @ being run as a formula when the file is opened in Excel."""
    text = "" if value is None else str(value)
    return "'" + text if text and text[0] in "=+-@\t\r" else text


@company_view
@admin_only
def export(request):
    _, entries = filtered(request.company, request.GET)
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="audit-log.csv"'
    writer = csv.writer(response)
    writer.writerow(["When", "Who", "IP address", "Action", "Area", "Item", "Employee", "Field", "Before", "After"])
    for e in entries[:EXPORT_LIMIT]:
        when = e.created_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        base = [when, e.who, e.ip_address or "", e.get_action_display(), e.model_name]
        if not e.changes:
            writer.writerow([_cell(c) for c in base + [e.object_repr, e.subject, "", "", ""]])
        for field, (before, after) in e.changes.items():
            writer.writerow([_cell(c) for c in base + [e.object_repr, e.subject, field, before, after]])
    return response
