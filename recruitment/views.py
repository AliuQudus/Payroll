import os

from django.contrib import messages
from django.db.models import Count, Q
from django.http import FileResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from payroll.views import company_view

from . import services
from .forms import ApplicantForm, HireForm, InterviewForm, ItemForm, OfferForm, VacancyForm
from .models import Applicant, Interview, Onboarding, OnboardingItem, Vacancy

Stage = Applicant.Stage


def _vacancy(request, pk):
    return get_object_or_404(Vacancy, pk=pk, company=request.company)


def _applicant(request, pk):
    return get_object_or_404(Applicant.objects.select_related("vacancy"), pk=pk, company=request.company)


def _back(applicant):
    return redirect("recruitment:applicant", applicant.pk)


def _attempt(request, action, success):
    """Run a service call, turning its ValueError into a message."""
    try:
        result = action()
    except ValueError as exc:
        messages.error(request, str(exc))
        return None
    messages.success(request, success)
    return result if result is not None else True


# ---------------------------------------------------------------- vacancies
@company_view
def vacancies(request):
    status = request.GET.get("status", "")
    rows = (Vacancy.objects.filter(company=request.company)
            .annotate(total=Count("applicants"), live=Count("applicants", filter=Q(applicants__stage__in=services.OPEN_STAGES)),
                      hired=Count("applicants", filter=Q(applicants__stage=Stage.HIRED))))
    if status in Vacancy.Status.values:
        rows = rows.filter(status=status)
    return render(request, "recruitment/vacancies.html", {"rows": rows, "status": status,
                                                          "statuses": Vacancy.Status.choices})


def _vacancy_form(request, title, instance):
    form = VacancyForm(request.POST or None, instance=instance)
    if request.method == "POST" and form.is_valid():
        vacancy = form.save(commit=False)
        vacancy.company = request.company
        if not vacancy.pk:
            vacancy.created_by = request.user
        vacancy.save()
        messages.success(request, "Vacancy saved.")
        return redirect("recruitment:vacancy", vacancy.pk)
    return render(request, "recruitment/form.html", {"title": title, "form": form,
                                                     "back_url": reverse("recruitment:vacancies")})


@company_view
def vacancy_new(request):
    return _vacancy_form(request, "New vacancy", Vacancy(company=request.company))


@company_view
def vacancy_edit(request, pk):
    vacancy = _vacancy(request, pk)
    return _vacancy_form(request, f"Edit vacancy: {vacancy.title}", vacancy)


@company_view
def vacancy_detail(request, pk):
    vacancy = _vacancy(request, pk)
    return render(request, "recruitment/vacancy.html", {
        "vacancy": vacancy, "groups": services.pipeline(vacancy),
        "hired": vacancy.applicants.filter(stage=Stage.HIRED).count()})


@company_view
@require_POST
def vacancy_status(request, pk, status):
    vacancy = _vacancy(request, pk)
    if status not in (Vacancy.Status.OPEN, Vacancy.Status.ON_HOLD, Vacancy.Status.CLOSED):
        messages.error(request, "Unknown status.")
    else:
        vacancy.status = status
        vacancy.save()
        messages.success(request, f"Vacancy is now {vacancy.get_status_display().lower()}.")
    return redirect("recruitment:vacancy", vacancy.pk)


# ---------------------------------------------------------------- applicants
@company_view
def applicant_new(request, vacancy_pk):
    vacancy = _vacancy(request, vacancy_pk)
    form = ApplicantForm(request.POST or None, request.FILES or None,
                         instance=Applicant(company=request.company, vacancy=vacancy))
    if request.method == "POST" and form.is_valid():
        data = {k: v for k, v in form.cleaned_data.items()}
        data["cv_name"] = data["cv"].name[:200] if data.get("cv") else ""
        if not data.get("cv"):
            data.pop("cv", None)
        applicant = _attempt(request, lambda: services.add_applicant(vacancy, request.user, **data),
                             "Applicant added.")
        if applicant:
            return _back(applicant)
    return render(request, "recruitment/form.html", {"title": f"New applicant for {vacancy.title}", "form": form,
                                                     "back_url": reverse("recruitment:vacancy", args=[vacancy.pk]),
                                                     "multipart": True})


@company_view
def applicant_edit(request, pk):
    applicant = _applicant(request, pk)
    form = ApplicantForm(request.POST or None, request.FILES or None, instance=applicant)
    if request.method == "POST" and form.is_valid():
        new_cv = request.FILES.get("cv")
        obj = form.save(commit=False)
        if new_cv:
            obj.cv_name = new_cv.name[:200]
        obj.save()
        messages.success(request, "Applicant updated.")
        return _back(applicant)
    return render(request, "recruitment/form.html", {"title": f"Edit {applicant}", "form": form,
                                                     "back_url": reverse("recruitment:applicant", args=[applicant.pk]),
                                                     "multipart": True})


@company_view
def applicant_detail(request, pk):
    applicant = _applicant(request, pk)
    offer = getattr(applicant, "offer", None)
    return render(request, "recruitment/applicant.html", {
        "a": applicant, "events": applicant.events.select_related("by"), "interviews": applicant.interviews.all(),
        "offer": offer, "open": applicant.stage in services.OPEN_STAGES,
        "can_screen": applicant.stage == Stage.APPLIED,
        "can_interview": applicant.stage in (Stage.APPLIED, Stage.SCREENING),
        "can_offer": applicant.stage in (Stage.APPLIED, Stage.SCREENING, Stage.INTERVIEW)
                     or (applicant.stage == Stage.OFFER and offer and offer.status == "PENDING"),
        "ready_to_hire": applicant.stage == Stage.OFFER and offer and offer.status == "ACCEPTED"
                         and not applicant.employee_id})


@company_view
@require_POST
def applicant_move(request, pk, stage):
    applicant = _applicant(request, pk)
    _attempt(request, lambda: services.move(applicant, stage, request.user, request.POST.get("note", "")),
             "Applicant moved.")
    return _back(applicant)


@company_view
@require_POST
def applicant_reject(request, pk):
    applicant = _applicant(request, pk)
    _attempt(request, lambda: services.reject(applicant, request.POST.get("reason", ""), request.user),
             "Applicant rejected.")
    return _back(applicant)


@company_view
@require_POST
def applicant_withdraw(request, pk):
    applicant = _applicant(request, pk)
    _attempt(request, lambda: services.withdraw(applicant, request.user), "Marked as withdrawn.")
    return _back(applicant)


@company_view
@require_POST
def applicant_reopen(request, pk):
    applicant = _applicant(request, pk)
    _attempt(request, lambda: services.reopen(applicant, request.user), "Applicant reconsidered.")
    return _back(applicant)


@company_view
def cv_download(request, pk):
    applicant = _applicant(request, pk)
    try:
        handle = applicant.cv.open("rb")
    except (FileNotFoundError, ValueError):
        messages.error(request, "There is no CV on file for this applicant.")
        return _back(applicant)
    return FileResponse(handle, as_attachment=True, filename=applicant.cv_name or os.path.basename(applicant.cv.name))


@company_view
@require_POST
def cv_delete(request, pk):
    applicant = _applicant(request, pk)
    if applicant.cv:
        applicant.cv.delete(save=False)
        applicant.cv, applicant.cv_name = "", ""
        applicant.save()
        messages.success(request, "CV deleted.")
    return _back(applicant)


# ---------------------------------------------------------------- interviews
@company_view
def interview_edit(request, applicant_pk=None, pk=None):
    if pk:
        interview = get_object_or_404(Interview.objects.select_related("applicant"), pk=pk, company=request.company)
        applicant = interview.applicant
    else:
        applicant = _applicant(request, applicant_pk)
        interview = Interview(company=request.company, applicant=applicant)
    form = InterviewForm(request.POST or None, instance=interview)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Interview saved.")
        return _back(applicant)
    return render(request, "recruitment/form.html", {"title": f"Interview: {applicant}", "form": form,
                                                     "back_url": reverse("recruitment:applicant", args=[applicant.pk])})


@company_view
@require_POST
def interview_delete(request, pk):
    interview = get_object_or_404(Interview.objects.select_related("applicant"), pk=pk, company=request.company)
    interview.delete()
    messages.success(request, "Interview removed.")
    return _back(interview.applicant)


# ---------------------------------------------------------------- offer and hire
@company_view
def offer_edit(request, pk):
    applicant = _applicant(request, pk)
    offer = getattr(applicant, "offer", None)
    initial = {} if offer else {"job_title": applicant.vacancy.title, "employment_type": applicant.vacancy.employment_type}
    form = OfferForm(request.POST or None, instance=offer, initial=initial)
    if request.method == "POST" and form.is_valid():
        saved = _attempt(request, lambda: services.save_offer(applicant, request.user, **form.cleaned_data),
                         "Offer saved. Record what the candidate says when they reply.")
        if saved:
            return _back(applicant)
    return render(request, "recruitment/form.html", {"title": f"Offer for {applicant}", "form": form,
                                                     "back_url": reverse("recruitment:applicant", args=[applicant.pk])})


@company_view
@require_POST
def offer_decide(request, pk, decision):
    applicant = _applicant(request, pk)
    _attempt(request, lambda: services.decide_offer(applicant, decision, request.user),
             {"accept": "Offer accepted. You can now hire them.", "decline": "Offer declined.",
              "withdraw": "Offer withdrawn."}.get(decision, "Done."))
    return _back(applicant)


@company_view
def hire(request, pk):
    applicant = _applicant(request, pk)
    offer = getattr(applicant, "offer", None)
    form = HireForm(request.POST or None, initial={"employee_no": services.suggest_employee_no(request.company),
                                                   "department": applicant.vacancy.department})
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        employee = _attempt(request, lambda: services.hire(applicant, d["employee_no"], request.user, d["department"]),
                            f"{applicant} is now an employee. Work through the onboarding checklist.")
        if employee:
            return redirect("recruitment:onboarding_detail", employee.onboarding.pk)
    return render(request, "recruitment/form.html", {
        "title": f"Hire {applicant}", "form": form, "back_url": reverse("recruitment:applicant", args=[applicant.pk]),
        "intro": (f"{offer.job_title}, starting {offer.start_date:%d %B %Y}, at {offer.gross_monthly:,.2f} a month." if offer else "")})


# ---------------------------------------------------------------- onboarding
def _onboarding(request, pk):
    return get_object_or_404(Onboarding.objects.select_related("employee"), pk=pk, company=request.company)


@company_view
def onboarding_list(request):
    rows = (Onboarding.objects.filter(company=request.company).select_related("employee")
            .annotate(total=Count("items"), done=Count("items", filter=Q(items__is_done=True))))
    return render(request, "recruitment/onboarding_list.html", {"rows": rows})


@company_view
def onboarding_detail(request, pk):
    ob = _onboarding(request, pk)
    return render(request, "recruitment/onboarding.html", {"ob": ob, "items": ob.items.all(), "item_form": ItemForm()})


@company_view
@require_POST
def item_add(request, pk):
    ob = _onboarding(request, pk)
    form = ItemForm(request.POST)
    if ob.status != Onboarding.Status.OPEN:
        messages.error(request, "This onboarding is complete.")
    elif form.is_valid():
        OnboardingItem.objects.create(company=request.company, onboarding=ob, name=form.cleaned_data["name"])
    else:
        messages.error(request, "Enter a name for the item.")
    return redirect("recruitment:onboarding_detail", ob.pk)


@company_view
@require_POST
def item_toggle(request, pk):
    item = get_object_or_404(OnboardingItem.objects.select_related("onboarding"), pk=pk, company=request.company)
    if item.onboarding.status != Onboarding.Status.OPEN:
        messages.error(request, "This onboarding is complete.")
    else:
        item.is_done = not item.is_done
        item.done_by, item.done_at = (request.user, timezone.now()) if item.is_done else (None, None)
        item.save()
    return redirect("recruitment:onboarding_detail", item.onboarding_id)


@company_view
@require_POST
def item_delete(request, pk):
    item = get_object_or_404(OnboardingItem.objects.select_related("onboarding"), pk=pk, company=request.company)
    if item.onboarding.status == Onboarding.Status.OPEN:
        item.delete()
    return redirect("recruitment:onboarding_detail", item.onboarding_id)


@company_view
@require_POST
def onboarding_complete(request, pk):
    ob = _onboarding(request, pk)
    _attempt(request, lambda: services.complete_onboarding(ob, request.user), "Onboarding complete.")
    return redirect("recruitment:onboarding_detail", ob.pk)
