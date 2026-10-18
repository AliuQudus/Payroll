from datetime import date

from django.conf import settings
from django.db import models

from payroll.models import CompanyOwned, Employee, Payslip


class LeaveType(CompanyOwned):
    name = models.CharField(max_length=100)
    days_per_year = models.PositiveSmallIntegerField(
        default=0, help_text="Days each employee gets per year. 0 means no limit.")
    is_paid = models.BooleanField(
        "paid leave", default=True, help_text="Untick if pay should be reduced for the days taken.")
    working_days_only = models.BooleanField(
        "count working days only", default=True,
        help_text="Skips weekends and public holidays when counting the days.")
    earns_allowance = models.BooleanField(
        "earns leave allowance", default=False,
        help_text="Approved leave of this type makes the employee eligible for the leave allowance in payroll.")
    is_active = models.BooleanField("active", default=True)

    class Meta:
        unique_together = [("company", "name")]
        ordering = ["name"]

    def __str__(self):
        return self.name


class PublicHoliday(CompanyOwned):
    date = models.DateField()
    name = models.CharField(max_length=100)

    class Meta:
        unique_together = [("company", "date")]
        ordering = ["date"]

    def __str__(self):
        return f"{self.name} ({self.date:%d %b %Y})"


class LeaveEntitlement(CompanyOwned):
    """Overrides the leave type's default days for one employee in one year, e.g. to carry days over."""
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="leave_entitlements")
    leave_type = models.ForeignKey(LeaveType, on_delete=models.CASCADE)
    year = models.PositiveSmallIntegerField()
    days = models.DecimalField("days for the year", max_digits=5, decimal_places=1)

    class Meta:
        unique_together = [("employee", "leave_type", "year")]
        ordering = ["-year", "employee_id"]


class LeaveRequest(CompanyOwned):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"
        CANCELLED = "CANCELLED", "Cancelled"

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="leave_requests")
    leave_type = models.ForeignKey(LeaveType, on_delete=models.PROTECT)
    start_date = models.DateField()
    end_date = models.DateField()
    days = models.DecimalField(max_digits=5, decimal_places=1, default=0, editable=False)
    reason = models.CharField(max_length=300, blank=True)
    relief_officer = models.ForeignKey(Employee, on_delete=models.SET_NULL, null=True, blank=True,
                                       related_name="+", help_text="Who covers while the employee is away.")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                     null=True, blank=True, related_name="+")
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-start_date"]

    def __str__(self):
        return f"{self.employee} {self.leave_type} {self.start_date:%d %b %Y}"

    @property
    def is_locked(self):
        """Decided requests are locked: they can be opened and read, but never edited."""
        return self.status != self.Status.PENDING

    @property
    def phase(self):
        if self.status != self.Status.APPROVED:
            return self.get_status_display()
        today = date.today()
        if self.end_date < today:
            return "Completed"
        return "On leave" if self.start_date <= today else "Approved"

    @property
    def allowance_paid(self):
        return self.allowance_applications.filter(payslip__run__status__in=["APPROVED", "PAID"]).exists()

    @property
    def allowance(self):
        if not self.leave_type.earns_allowance:
            return "Not applicable"
        if self.allowance_paid:
            return "Paid"
        return "Due in next payroll" if self.status == self.Status.APPROVED else "-"


class LeaveAllowanceApplication(models.Model):
    """Records the leave allowance paid on a payslip, so it is paid once per leave year."""
    leave_request = models.ForeignKey(LeaveRequest, on_delete=models.PROTECT, related_name="allowance_applications")
    payslip = models.ForeignKey(Payslip, on_delete=models.CASCADE, related_name="leave_applications")
    amount = models.DecimalField(max_digits=14, decimal_places=2)
