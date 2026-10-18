"""Employee records: who someone is, where they sit, their emergency contacts and their documents."""
import os
import uuid
from datetime import date

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.signals import post_delete
from django.dispatch import receiver

from payroll.models import Employee


class EmployeeProfile(models.Model):
    """HR details for one payroll employee. Payroll never needs this to run."""

    class EmploymentType(models.TextChoices):
        PERMANENT = "PERMANENT", "Permanent"
        CONTRACT = "CONTRACT", "Fixed-term contract"
        PART_TIME = "PART_TIME", "Part-time"
        INTERN = "INTERN", "Intern or corps member"

    class Gender(models.TextChoices):
        MALE = "M", "Male"
        FEMALE = "F", "Female"

    class Marital(models.TextChoices):
        SINGLE = "SINGLE", "Single"
        MARRIED = "MARRIED", "Married"
        DIVORCED = "DIVORCED", "Divorced"
        WIDOWED = "WIDOWED", "Widowed"

    employee = models.OneToOneField(Employee, on_delete=models.CASCADE, related_name="profile")
    job_title = models.CharField(max_length=100)
    employment_type = models.CharField(max_length=10, choices=EmploymentType.choices,
                                       default=EmploymentType.PERMANENT)
    hire_date = models.DateField()
    probation_end_date = models.DateField(null=True, blank=True)
    contract_end_date = models.DateField(null=True, blank=True)
    manager = models.ForeignKey(Employee, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name="direct_reports", verbose_name="reports to")
    date_of_birth = models.DateField(null=True, blank=True)
    gender = models.CharField(max_length=1, choices=Gender.choices, blank=True)
    marital_status = models.CharField(max_length=10, choices=Marital.choices, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    address = models.CharField(max_length=300, blank=True)
    tax_id = models.CharField("Tax identification number (TIN)", max_length=30, blank=True)
    rsa_pin = models.CharField("Pension RSA PIN", max_length=30, blank=True)
    pension_administrator = models.CharField("Pension fund administrator", max_length=100, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.employee}: {self.job_title}"

    def clean(self):
        errors = {}
        hired = self.hire_date
        if hired:
            if self.probation_end_date and self.probation_end_date < hired:
                errors["probation_end_date"] = "Probation cannot end before the hire date."
            if self.contract_end_date and self.contract_end_date < hired:
                errors["contract_end_date"] = "The contract cannot end before the hire date."
        if self.date_of_birth:
            if self.date_of_birth >= date.today():
                errors["date_of_birth"] = "Date of birth must be in the past."
            elif hired and self.date_of_birth >= hired:
                errors["date_of_birth"] = "Date of birth must be before the hire date."
        if self.employee_id and self.manager_id:
            if self.manager_id == self.employee_id:
                errors["manager"] = "An employee cannot report to themselves."
            elif self.manager.company_id != self.employee.company_id:
                errors["manager"] = "The manager must work in the same company."
            else:
                seen, nxt = {self.employee_id}, self.manager_id
                while nxt:                       # walk up the chain: nobody may end up managing their own manager
                    if nxt in seen:
                        errors["manager"] = "That would create a reporting loop."
                        break
                    seen.add(nxt)
                    nxt = (EmployeeProfile.objects.filter(employee_id=nxt)
                           .values_list("manager_id", flat=True).first())
        if errors:
            raise ValidationError(errors)


class EmergencyContact(models.Model):
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="emergency_contacts")
    name = models.CharField(max_length=150)
    relationship = models.CharField(max_length=60)
    phone = models.CharField(max_length=30)
    address = models.CharField(max_length=300, blank=True)
    is_next_of_kin = models.BooleanField("next of kin", default=False)

    class Meta:
        ordering = ["-is_next_of_kin", "name"]

    def __str__(self):
        return f"{self.name} ({self.relationship})"


def document_path(instance, filename):
    """Stored under a random name so uploads can never collide or escape the folder."""
    ext = os.path.splitext(filename)[1].lower()
    emp = instance.employee
    return f"hr_documents/{emp.company_id}/{emp.pk}/{uuid.uuid4().hex}{ext}"


class EmployeeDocument(models.Model):
    class Kind(models.TextChoices):
        OFFER = "OFFER", "Offer letter"
        CONTRACT = "CONTRACT", "Employment contract"
        ID = "ID", "Identification"
        CERTIFICATE = "CERTIFICATE", "Certificate or qualification"
        GUARANTOR = "GUARANTOR", "Guarantor form"
        OTHER = "OTHER", "Other"

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="documents")
    kind = models.CharField("type", max_length=12, choices=Kind.choices, default=Kind.OTHER)
    title = models.CharField(max_length=150)
    file = models.FileField(upload_to=document_path)
    original_name = models.CharField(max_length=200, blank=True, editable=False)
    issue_date = models.DateField(null=True, blank=True)
    expiry_date = models.DateField(null=True, blank=True)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name="+")
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-uploaded_at"]

    def __str__(self):
        return self.title

    @property
    def is_expired(self):
        return bool(self.expiry_date and self.expiry_date < date.today())


@receiver(post_delete, sender=EmployeeDocument)
def remove_file(sender, instance, **kwargs):
    """Deleting a document record also removes the file from disk."""
    if instance.file:
        instance.file.delete(save=False)
