import csv
from datetime import date

from django.contrib import messages
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from payroll.models import PayrollRun
from payroll.views import admin_only, company_view

from . import services
from .forms import DeadlineForm, PaymentForm
from .models import Authority, RemittancePayment, RemittanceSetting

SLUGS = {"paye": Authority.PAYE, "pension": Authority.PENSION}


def _slug(authority):
    return authority.lower()


def _context(request, run_pk, slug):
    """The run and authority from the URL, only if they belong to this company and the run is approved or paid."""
    if slug not in SLUGS:
        raise Http404
    run = get_object_or_404(PayrollRun, pk=run_pk, company=request.company, status__in=services.FINAL_STATUSES)
    return run, SLUGS[slug]


def _cell(value):
    """Stop a name that starts with = + - or @ being run as a formula when the file is opened in Excel."""
    text = str(value)
    return "'" + text if text and text[0] in "=+-@\t\r" else text


@company_view
def overview(request):
    rows = services.position(request.company)
    return render(request, "remittances/overview.html", {
        "rows": rows, "summary": services.summary(rows), "slug": _slug})


@company_view
def schedule(request, run_pk, authority):
    run, auth = _context(request, run_pk, authority)
    days = services.due_days(request.company)
    due = services.amounts_due([run])[(run.pk, auth)]
    paid = services.amounts_paid([run])[(run.pk, auth)]
    deadline = services.due_date(run, days[auth])
    ctx = {"run": run, "auth": auth, "label": Authority(auth).label, "slug": authority,
           "due": due, "paid": paid, "outstanding": max(due - paid, services.ZERO), "deadline": deadline,
           "status": services.status_of(due, paid, deadline, date.today()),
           "payments": RemittancePayment.objects.filter(run=run, authority=auth).select_related("recorded_by")}
    ctx["data"] = services.paye_schedule(run) if auth == Authority.PAYE else services.pension_schedule(run)
    return render(request, "remittances/schedule.html", ctx)


@company_view
def schedule_csv(request, run_pk, authority):
    run, auth = _context(request, run_pk, authority)
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{authority}-{run.year}-{run.month:02d}.csv"'
    writer = csv.writer(response)
    if auth == Authority.PAYE:
        data = services.paye_schedule(run)
        writer.writerow(["Employee no", "Name", "TIN", "Gross pay", "PAYE"])
        for r in data["rows"]:
            e = r["employee"]
            writer.writerow([_cell(e.employee_no), _cell(f"{e.first_name} {e.last_name}"), _cell(r["tin"]),
                             r["gross"], r["paye"]])
        writer.writerow(["", "Total", "", "", data["total"]])
    else:
        data = services.pension_schedule(run)
        writer.writerow(["Pension administrator", "Employee no", "Name", "RSA PIN", "Employee contribution",
                         "Employer contribution", "Total"])
        for g in data["groups"]:
            for r in g["rows"]:
                e = r["employee"]
                writer.writerow([_cell(g["pfa"]), _cell(e.employee_no), _cell(f"{e.first_name} {e.last_name}"),
                                 _cell(r["rsa_pin"]), r["ee"], r["er"], r["total"]])
        writer.writerow(["Total", "", "", "", data["ee"], data["er"], data["total"]])
    return response


@company_view
def payment_new(request, run_pk, authority):
    run, auth = _context(request, run_pk, authority)
    left = services.outstanding(run, auth)
    if left <= 0:
        messages.info(request, "There is nothing left to pay for this schedule.")
        return redirect("remittances:schedule", run.pk, authority)
    form = PaymentForm(request.POST or None, instance=RemittancePayment(company=request.company, run=run,
                                                                         authority=auth),
                       run=run, authority=auth, outstanding=left)
    if request.method == "POST" and form.is_valid():
        payment = form.save(commit=False)
        payment.recorded_by = request.user
        payment.save()
        messages.success(request, f"Payment of ₦{payment.amount:,.2f} recorded.")
        return redirect("remittances:schedule", run.pk, authority)
    return render(request, "remittances/form.html", {
        "title": f"Record a {Authority(auth).label} payment for {run.year}-{run.month:02d}", "form": form,
        "back_url": reverse("remittances:schedule", args=[run.pk, authority])})


@company_view
@require_POST
def payment_delete(request, pk):
    payment = get_object_or_404(RemittancePayment, pk=pk, company=request.company)
    run_pk, slug = payment.run_id, _slug(payment.authority)
    payment.delete()
    messages.success(request, "Payment deleted.")
    return redirect("remittances:schedule", run_pk, slug)


@company_view
@admin_only
def deadlines(request):
    current = services.due_days(request.company)
    form = DeadlineForm(request.POST or None, initial={"paye_day": current[Authority.PAYE],
                                                       "pension_day": current[Authority.PENSION]})
    if request.method == "POST" and form.is_valid():
        for authority, field in ((Authority.PAYE, "paye_day"), (Authority.PENSION, "pension_day")):
            RemittanceSetting.objects.update_or_create(company=request.company, authority=authority,
                                                       defaults={"due_day": form.cleaned_data[field]})
        messages.success(request, "Deadlines saved.")
        return redirect("remittances:overview")
    return render(request, "remittances/form.html", {
        "title": "Remittance deadlines", "form": form, "back_url": reverse("remittances:overview"),
        "intro": "The day of the month after payroll by which each payment is due. The starting values are "
                 "assumptions: confirm the deadlines that apply to your company with the authorities."})
