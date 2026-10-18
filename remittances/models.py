"""What was paid to the tax authority and the pension administrators, and when it is due.

The amounts owed are never stored: they are read from approved payslips every time, so they cannot drift.
Only the facts that cannot be derived are saved here: the deadlines the company works to and each payment made.
"""
from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from payroll.models import CompanyOwned, PayrollRun


class Authority(models.TextChoices):
    PAYE = "PAYE", "PAYE (tax authority)"
    PENSION = "PENSION", "Pension (administrators)"


class RemittanceSetting(CompanyOwned):
    """The day of the following month by which an authority must be paid."""
    authority = models.CharField(max_length=10, choices=Authority.choices)
    due_day = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(28)])

    class Meta:
        unique_together = [("company", "authority")]


class RemittancePayment(CompanyOwned):
    """One payment. Pension is normally several, one per administrator; any amount can be paid in parts."""
    run = models.ForeignKey(PayrollRun, on_delete=models.PROTECT, related_name="remittance_payments")
    authority = models.CharField(max_length=10, choices=Authority.choices)
    paid_date = models.DateField()
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    payee = models.CharField("paid to", max_length=150, blank=True,
                             help_text="For example the tax office or the pension administrator.")
    reference = models.CharField("payment reference", max_length=100, blank=True)
    note = models.CharField(max_length=300, blank=True)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["paid_date", "id"]

    def __str__(self):
        return f"{self.get_authority_display()} {self.run.year}-{self.run.month:02d}: {self.amount}"
