"""Recruitment rules: how applicants move, offers, and turning an accepted offer into an employee."""
import calendar
import re
from datetime import date

from django.apps import apps
from django.db import transaction
from django.utils import timezone

from payroll.models import Employee

from .models import Applicant, Interview, Offer, Onboarding, OnboardingItem, StageEvent, Vacancy

Stage, OfferStatus = Applicant.Stage, Offer.Status
OPEN_STAGES = [Stage.APPLIED, Stage.SCREENING, Stage.INTERVIEW, Stage.OFFER]
DEFAULT_TASKS = [
    "Employment contract signed", "ID and certificates collected", "Bank account details collected and entered in payroll",
    "Tax ID (TIN) and pension RSA PIN collected", "Guarantor forms collected", "Equipment issued",
    "Email and system access set up", "Induction held",
]


def _event(applicant, stage, note="", user=None):
    StageEvent.objects.create(company=applicant.company, applicant=applicant, stage=stage, note=note[:300], by=user)


# ---------------------------------------------------------------- applicants
@transaction.atomic
def add_applicant(vacancy, user=None, **fields):
    if vacancy.status != Vacancy.Status.OPEN:
        raise ValueError(f"'{vacancy.title}' is not open for applications.")
    email = (fields.get("email") or "").strip()
    if email and Applicant.objects.filter(vacancy=vacancy, email__iexact=email).exists():
        raise ValueError(f"Someone with the email {email} has already applied for this vacancy.")
    applicant = Applicant.objects.create(company=vacancy.company, vacancy=vacancy, created_by=user, **fields)
    _event(applicant, Stage.APPLIED, "Application received.", user)
    return applicant


@transaction.atomic
def move(applicant, stage, user=None, note=""):
    """Advance an applicant through screening and interview. Offers and hiring have their own steps."""
    if applicant.stage not in OPEN_STAGES:
        raise ValueError(f"{applicant} is {applicant.get_stage_display().lower()}, so cannot be moved.")
    if stage not in (Stage.SCREENING, Stage.INTERVIEW):
        raise ValueError("An offer is made from the applicant's page, and hiring follows an accepted offer.")
    if OPEN_STAGES.index(stage) <= OPEN_STAGES.index(applicant.stage):
        raise ValueError(f"{applicant} is already past {Stage(stage).label.lower()}.")
    applicant.stage = stage
    applicant.save()
    _event(applicant, stage, note, user)


def _close_offer(applicant):
    offer = getattr(applicant, "offer", None)
    if offer is not None and offer.status in (OfferStatus.PENDING, OfferStatus.ACCEPTED):
        offer.status, offer.decided_on = OfferStatus.WITHDRAWN, date.today()
        offer.save()


@transaction.atomic
def reject(applicant, reason, user=None):
    if applicant.stage not in OPEN_STAGES:
        raise ValueError(f"{applicant} is {applicant.get_stage_display().lower()}, so cannot be rejected.")
    if not reason.strip():
        raise ValueError("Give a reason, so the record shows why.")
    _close_offer(applicant)
    applicant.stage, applicant.rejection_reason = Stage.REJECTED, reason.strip()[:300]
    applicant.save()
    _event(applicant, Stage.REJECTED, reason, user)


@transaction.atomic
def withdraw(applicant, user=None, note="Withdrew from the process."):
    if applicant.stage not in OPEN_STAGES:
        raise ValueError(f"{applicant} is {applicant.get_stage_display().lower()}, so cannot withdraw.")
    _close_offer(applicant)
    applicant.stage = Stage.WITHDRAWN
    applicant.save()
    _event(applicant, Stage.WITHDRAWN, note, user)


@transaction.atomic
def reopen(applicant, user=None):
    if applicant.stage not in (Stage.REJECTED, Stage.WITHDRAWN):
        raise ValueError("Only a rejected or withdrawn applicant can be reconsidered.")
    if applicant.vacancy.status != Vacancy.Status.OPEN:
        raise ValueError("The vacancy is not open.")
    applicant.stage, applicant.rejection_reason = Stage.APPLIED, ""
    applicant.save()
    _event(applicant, Stage.APPLIED, "Reconsidered.", user)


# ---------------------------------------------------------------- offers
@transaction.atomic
def save_offer(applicant, user=None, **fields):
    """Create or change the offer. Making one moves the applicant to the offer stage."""
    if applicant.stage not in OPEN_STAGES:
        raise ValueError(f"{applicant} is {applicant.get_stage_display().lower()}, so cannot be made an offer.")
    if not 0 <= fields["probation_months"] <= 12:
        raise ValueError("Probation is between 0 and 12 months.")
    if fields["start_date"] < applicant.applied_on:
        raise ValueError("The start date cannot be before the application date.")
    offer = getattr(applicant, "offer", None)
    if offer is not None and offer.status != OfferStatus.PENDING and offer.status != OfferStatus.WITHDRAWN:
        raise ValueError(f"The offer was {offer.get_status_display().lower()}, so cannot be changed.")
    if offer is None:
        offer = Offer(company=applicant.company, applicant=applicant, created_by=user)
    for name, value in fields.items():
        setattr(offer, name, value)
    offer.status, offer.decided_on = OfferStatus.PENDING, None
    offer.save()
    if applicant.stage != Stage.OFFER:
        applicant.stage = Stage.OFFER
        applicant.save()
        _event(applicant, Stage.OFFER, f"Offer made: {offer.job_title}.", user)
    return offer


@transaction.atomic
def decide_offer(applicant, decision, user=None, on=None):
    """Record what the candidate said. A decline ends the application; withdrawing goes back to interview."""
    offer = getattr(applicant, "offer", None)
    if applicant.stage != Stage.OFFER or offer is None or offer.status != OfferStatus.PENDING:
        raise ValueError("There is no offer waiting for a decision.")
    offer.decided_on = on or date.today()
    if decision == "accept":
        offer.status = OfferStatus.ACCEPTED
        _event(applicant, Stage.OFFER, "Offer accepted.", user)
    elif decision == "decline":
        offer.status = OfferStatus.DECLINED
        applicant.stage = Stage.WITHDRAWN
        applicant.save()
        _event(applicant, Stage.WITHDRAWN, "Declined the offer.", user)
    elif decision == "withdraw":
        offer.status = OfferStatus.WITHDRAWN
        applicant.stage = Stage.INTERVIEW
        applicant.save()
        _event(applicant, Stage.INTERVIEW, "Offer withdrawn by the company.", user)
    else:
        raise ValueError("Unknown decision.")
    offer.save()


# ---------------------------------------------------------------- hiring
def suggest_employee_no(company):
    """The next number in the company's own style: E041 follows E040, EMP-007 follows EMP-006."""
    best = None
    for number in Employee.objects.filter(company=company).values_list("employee_no", flat=True):
        match = re.fullmatch(r"(\D*)(\d+)", number)
        if match and (best is None or int(match.group(2)) > best[0]):
            best = (int(match.group(2)), match.group(1), len(match.group(2)))
    if best is None:
        return "E001"
    value, prefix, width = best
    return f"{prefix}{str(value + 1).zfill(width)}"


def add_months(day, months):
    index = day.year * 12 + day.month - 1 + months
    year, month = divmod(index, 12)
    return date(year, month + 1, min(day.day, calendar.monthrange(year, month + 1)[1]))


@transaction.atomic
def hire(applicant, employee_no, user=None, department=""):
    """Turn an accepted offer into an employee, an HR record (when HR is installed) and an onboarding checklist."""
    offer = getattr(applicant, "offer", None)
    if applicant.employee_id:
        raise ValueError(f"{applicant} has already been hired.")
    if applicant.stage != Stage.OFFER or offer is None or offer.status != OfferStatus.ACCEPTED:
        raise ValueError("Only an applicant who has accepted an offer can be hired.")
    employee_no = employee_no.strip()
    if not employee_no:
        raise ValueError("Enter an employee number.")
    if Employee.objects.filter(company=applicant.company, employee_no__iexact=employee_no).exists():
        raise ValueError(f"The employee number {employee_no} is already in use.")
    employee = Employee.objects.create(
        company=applicant.company, employee_no=employee_no, first_name=applicant.first_name,
        last_name=applicant.last_name, email=applicant.email, department=department or applicant.vacancy.department,
        gross_monthly=offer.gross_monthly)
    if apps.is_installed("hr"):
        from hr.models import EmployeeProfile
        EmployeeProfile.objects.create(
            employee=employee, job_title=offer.job_title, employment_type=offer.employment_type,
            hire_date=offer.start_date, phone=applicant.phone,
            probation_end_date=add_months(offer.start_date, offer.probation_months) if offer.probation_months else None)
    onboarding = Onboarding.objects.create(company=applicant.company, employee=employee, applicant=applicant,
                                           start_date=offer.start_date)
    tasks = list(DEFAULT_TASKS) + (["Employee portal access given"] if apps.is_installed("portal") else [])
    OnboardingItem.objects.bulk_create([OnboardingItem(company=applicant.company, onboarding=onboarding, name=t)
                                        for t in tasks])
    applicant.stage, applicant.employee = Stage.HIRED, employee
    applicant.save()
    _event(applicant, Stage.HIRED, f"Hired as {employee_no}, starting {offer.start_date:%d %b %Y}.", user)
    vacancy = applicant.vacancy
    if (vacancy.status == Vacancy.Status.OPEN
            and Applicant.objects.filter(vacancy=vacancy, stage=Stage.HIRED).count() >= vacancy.headcount):
        vacancy.status = Vacancy.Status.FILLED
        vacancy.save()
    return employee


@transaction.atomic
def complete_onboarding(onboarding, user):
    if onboarding.status != Onboarding.Status.OPEN:
        raise ValueError("This onboarding is already complete.")
    undone = onboarding.items.filter(is_done=False).count()
    if undone:
        raise ValueError(f"{undone} item{'s are' if undone != 1 else ' is'} still to do.")
    onboarding.status, onboarding.completed_by, onboarding.completed_at = Onboarding.Status.COMPLETE, user, timezone.now()
    onboarding.save()


# ---------------------------------------------------------------- reading
def pipeline(vacancy):
    """Applicants of a vacancy grouped by stage, in pipeline order, then the ones who left the process."""
    groups = []
    people = list(vacancy.applicants.all())
    for stage in [Stage.APPLIED, Stage.SCREENING, Stage.INTERVIEW, Stage.OFFER, Stage.HIRED, Stage.REJECTED,
                  Stage.WITHDRAWN]:
        members = [a for a in people if a.stage == stage]
        groups.append((stage, Stage(stage).label, members))
    return groups
