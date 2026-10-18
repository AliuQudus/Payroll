"""Pay grades and the dated history of every employee's monthly gross salary."""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from payroll.models import CompanyOwned, Employee, Payslip


class PayGrade(CompanyOwned):
    name = models.CharField(max_length=60)
    min_gross = models.DecimalField("lowest monthly gross", max_digits=14, decimal_places=2)
    max_gross = models.DecimalField("highest monthly gross", max_digits=14, decimal_places=2)

    class Meta:
        unique_together = [("company", "name")]
        ordering = ["min_gross", "name"]

    def __str__(self):
        return self.name

    def clean(self):
        if self.min_gross is not None and self.max_gross is not None and self.max_gross < self.min_gross:
            raise ValidationError({"max_gross": "The highest salary cannot be below the lowest."})

    def contains(self, gross):
        return self.min_gross <= gross <= self.max_gross


class SalaryRevision(CompanyOwned):
    """One change of monthly gross. It applies to the whole payroll month that effective_from falls in."""

    class Reason(models.TextChoices):
        OPENING = "OPENING", "Opening salary"
        PROMOTION = "PROMOTION", "Promotion"
        REVIEW = "REVIEW", "Annual review"
        ADJUSTMENT = "ADJUSTMENT", "Market adjustment"
        CORRECTION = "CORRECTION", "Correction"
        OTHER = "OTHER", "Other"

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="salary_revisions")
    effective_from = models.DateField(help_text="Applies to the whole payroll month this date falls in.")
    gross_monthly = models.DecimalField("new monthly gross", max_digits=14, decimal_places=2)
    reason = models.CharField(max_length=12, choices=Reason.choices, default=Reason.REVIEW)
    grade = models.ForeignKey(PayGrade, on_delete=models.PROTECT, null=True, blank=True, related_name="revisions")
    note = models.CharField(max_length=300, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("employee", "effective_from")]
        ordering = ["effective_from"]

    def __str__(self):
        return f"{self.employee}: {self.gross_monthly} from {self.effective_from:%b %Y}"

    def clean(self):
        if self.gross_monthly is not None and self.gross_monthly <= 0:
            raise ValidationError({"gross_monthly": "The salary must be above zero."})
        if self.effective_from and self.effective_from.day != 1:
            raise ValidationError({"effective_from": "A change starts at the beginning of a payroll month."})


class SalaryBasis(models.Model):
    """The monthly gross a payslip was actually based on, before any absence reduction."""
    payslip = models.OneToOneField(Payslip, on_delete=models.CASCADE, related_name="salary_basis")
    monthly_gross = models.DecimalField(max_digits=14, decimal_places=2)


class ArrearsApplication(models.Model):
    """Back pay added to a later payslip to make up a shortfall on an earlier one."""
    basis = models.ForeignKey(SalaryBasis, on_delete=models.CASCADE, related_name="arrears")
    payslip = models.ForeignKey(Payslip, on_delete=models.CASCADE, related_name="+")
    amount = models.DecimalField(max_digits=14, decimal_places=2)
