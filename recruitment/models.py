"""Vacancies, applicants through to a hire, and the onboarding checklist for the people hired."""
import os
import uuid
from datetime import date
from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q
from django.db.models.signals import post_delete
from django.dispatch import receiver

from payroll.models import CompanyOwned, Employee

# the same values the HR module uses, so a hire carries across unchanged
EMPLOYMENT_TYPES = [("PERMANENT", "Permanent"), ("CONTRACT", "Fixed-term contract"), ("PART_TIME", "Part-time"),
                    ("INTERN", "Intern or corps member")]


class Vacancy(CompanyOwned):
    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        ON_HOLD = "ON_HOLD", "On hold"
        FILLED = "FILLED", "Filled"
        CLOSED = "CLOSED", "Closed"

    title = models.CharField(max_length=120)
    department = models.CharField(max_length=100, blank=True)
    employment_type = models.CharField(max_length=10, choices=EMPLOYMENT_TYPES, default="PERMANENT")
    headcount = models.PositiveSmallIntegerField("positions to fill", default=1, validators=[MinValueValidator(1)])
    description = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    opened_on = models.DateField(default=date.today)
    closing_date = models.DateField(null=True, blank=True, help_text="Applications close on this date. Optional.")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["status", "-opened_on", "-id"]
        verbose_name_plural = "vacancies"

    def __str__(self):
        return self.title


def cv_path(instance, filename):
    """Stored under a random name so uploads can never collide or escape the folder."""
    return f"recruitment/{instance.company_id}/{uuid.uuid4().hex}{os.path.splitext(filename)[1].lower()}"


class Applicant(CompanyOwned):
    class Stage(models.TextChoices):
        APPLIED = "APPLIED", "Applied"
        SCREENING = "SCREENING", "Screening"
        INTERVIEW = "INTERVIEW", "Interview"
        OFFER = "OFFER", "Offer"
        HIRED = "HIRED", "Hired"
        REJECTED = "REJECTED", "Rejected"
        WITHDRAWN = "WITHDRAWN", "Withdrawn"

    class Source(models.TextChoices):
        REFERRAL = "REFERRAL", "Referral"
        JOB_BOARD = "JOB_BOARD", "Job board"
        WALK_IN = "WALK_IN", "Walk-in"
        AGENCY = "AGENCY", "Agency"
        OTHER = "OTHER", "Other"

    vacancy = models.ForeignKey(Vacancy, on_delete=models.PROTECT, related_name="applicants")
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=30, blank=True)
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.OTHER)
    stage = models.CharField(max_length=10, choices=Stage.choices, default=Stage.APPLIED)
    notes = models.TextField(blank=True)
    cv = models.FileField("CV", upload_to=cv_path, blank=True)
    cv_name = models.CharField(max_length=200, blank=True, editable=False)
    applied_on = models.DateField(default=date.today)
    rejection_reason = models.CharField(max_length=300, blank=True)
    employee = models.ForeignKey(Employee, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-applied_on", "-id"]
        constraints = [models.UniqueConstraint(fields=["vacancy", "email"], condition=~Q(email=""),
                                               name="one_application_per_email_per_vacancy")]

    def __str__(self):
        return f"{self.first_name} {self.last_name}"


@receiver(post_delete, sender=Applicant)
def remove_cv(sender, instance, **kwargs):
    if instance.cv:
        instance.cv.delete(save=False)


class StageEvent(CompanyOwned):
    """The history of an applicant: each step, who took it and why."""
    applicant = models.ForeignKey(Applicant, on_delete=models.CASCADE, related_name="events")
    stage = models.CharField(max_length=10, choices=Applicant.Stage.choices)
    note = models.CharField(max_length=300, blank=True)
    by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                           related_name="+")
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["at", "id"]


class Interview(CompanyOwned):
    applicant = models.ForeignKey(Applicant, on_delete=models.CASCADE, related_name="interviews")
    when = models.DateTimeField()
    interviewer = models.CharField(max_length=100)
    feedback = models.TextField(blank=True)
    rating = models.PositiveSmallIntegerField(null=True, blank=True, help_text="1 (weak) to 5 (excellent). Optional.")

    class Meta:
        ordering = ["when"]


class Offer(CompanyOwned):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Waiting for the candidate"
        ACCEPTED = "ACCEPTED", "Accepted"
        DECLINED = "DECLINED", "Declined"
        WITHDRAWN = "WITHDRAWN", "Withdrawn"

    applicant = models.OneToOneField(Applicant, on_delete=models.CASCADE, related_name="offer")
    job_title = models.CharField(max_length=100)
    gross_monthly = models.DecimalField("monthly gross", max_digits=14, decimal_places=2,
                                        validators=[MinValueValidator(Decimal("0.01"))])
    start_date = models.DateField()
    employment_type = models.CharField(max_length=10, choices=EMPLOYMENT_TYPES, default="PERMANENT")
    probation_months = models.PositiveSmallIntegerField(default=0, help_text="0 for no probation. Up to 12.")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    decided_on = models.DateField(null=True, blank=True)
    notes = models.CharField(max_length=300, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)


class Onboarding(CompanyOwned):
    class Status(models.TextChoices):
        OPEN = "OPEN", "In progress"
        COMPLETE = "COMPLETE", "Complete"

    employee = models.OneToOneField(Employee, on_delete=models.CASCADE, related_name="onboarding")
    applicant = models.ForeignKey(Applicant, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    start_date = models.DateField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    completed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name="+")
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["status", "-start_date"]


class OnboardingItem(CompanyOwned):
    onboarding = models.ForeignKey(Onboarding, on_delete=models.CASCADE, related_name="items")
    name = models.CharField(max_length=150)
    is_done = models.BooleanField(default=False)
    done_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name="+")
    done_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["id"]
