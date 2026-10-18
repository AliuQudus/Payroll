"""An employee leaving: the exit itself, the clearance checklist and the lines of the final settlement."""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from payroll.models import CompanyOwned, Employee


class Exit(CompanyOwned):
    class Reason(models.TextChoices):
        RESIGNATION = "RESIGNATION", "Resignation"
        TERMINATION = "TERMINATION", "Termination by the employer"
        CONTRACT_END = "CONTRACT_END", "End of contract"
        RETIREMENT = "RETIREMENT", "Retirement"
        REDUNDANCY = "REDUNDANCY", "Redundancy"
        OTHER = "OTHER", "Other"

    class Status(models.TextChoices):
        OPEN = "OPEN", "In progress"
        COMPLETED = "COMPLETED", "Completed"
        CANCELLED = "CANCELLED", "Cancelled"

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="exits")
    reason = models.CharField(max_length=12, choices=Reason.choices)
    notice_date = models.DateField(null=True, blank=True, help_text="The day notice was given or received.")
    last_working_day = models.DateField(
        help_text="Pay stops after this day. The final month is paid for the days up to and including it.")
    notes = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    completed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name="+")
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-last_working_day", "-id"]
        constraints = [models.UniqueConstraint(fields=["employee"], condition=Q(status="OPEN"),
                                               name="one_open_exit_per_employee")]

    def __str__(self):
        return f"{self.employee}: leaving {self.last_working_day:%d %b %Y}"

    def clean(self):
        if self.notice_date and self.last_working_day and self.last_working_day < self.notice_date:
            raise ValidationError({"last_working_day": "The last working day cannot be before the notice date."})


class ClearanceItem(models.Model):
    exit = models.ForeignKey(Exit, on_delete=models.CASCADE, related_name="items")
    name = models.CharField(max_length=150)
    is_done = models.BooleanField(default=False)
    done_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name="+")
    done_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return self.name


class SettlementLine(models.Model):
    """A payment to, or deduction from, the final pay. Added to the payslip of the employee's last month."""
    EARNING, DEDUCTION = "EARNING", "DEDUCTION"
    KINDS = [(EARNING, "Payment to the employee"), (DEDUCTION, "Deduction from pay")]

    exit = models.ForeignKey(Exit, on_delete=models.CASCADE, related_name="lines")
    kind = models.CharField(max_length=10, choices=KINDS, default=EARNING)
    description = models.CharField(max_length=100)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    taxable = models.BooleanField(
        default=True, help_text="Tick if the payment is taxable. Confirm the treatment of exit payments with "
                                "your tax adviser: some compensation for loss of employment may be exempt.")

    class Meta:
        ordering = ["kind", "id"]

    def __str__(self):
        return f"{self.description}: {self.amount}"

    def clean(self):
        if self.amount is not None and self.amount <= 0:
            raise ValidationError({"amount": "Enter an amount above zero."})
