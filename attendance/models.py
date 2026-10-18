"""The daily attendance register, overtime requests and the company's overtime rules."""
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models

from payroll.models import CompanyOwned, Employee, Payslip


class AttendanceSetting(CompanyOwned):
    """How overtime is priced. One row per company; a company without one uses these defaults."""
    standard_daily_hours = models.DecimalField("standard hours in a working day", max_digits=4, decimal_places=2,
                                               default=Decimal("8"), validators=[MinValueValidator(Decimal("1"))])
    hours_per_month = models.DecimalField(
        "working hours in a month", max_digits=6, decimal_places=2, default=Decimal("173.33"),
        validators=[MinValueValidator(Decimal("1"))],
        help_text="The hourly rate is the pay base divided by this. 173.33 is a 40-hour week.")
    base_percent = models.DecimalField(
        "share of monthly gross used as the overtime base", max_digits=5, decimal_places=2, default=Decimal("100"),
        validators=[MinValueValidator(Decimal("1"))],
        help_text="100 means the whole monthly gross. Use a lower figure if overtime is worked out on basic pay.")
    weekday_rate = models.DecimalField("weekday rate (times the hourly rate)", max_digits=4, decimal_places=2,
                                       default=Decimal("1.5"), validators=[MinValueValidator(Decimal("1"))])
    weekend_rate = models.DecimalField("weekend rate", max_digits=4, decimal_places=2, default=Decimal("2"),
                                       validators=[MinValueValidator(Decimal("1"))])
    holiday_rate = models.DecimalField("public holiday rate", max_digits=4, decimal_places=2, default=Decimal("2"),
                                       validators=[MinValueValidator(Decimal("1"))])

    class Meta:
        constraints = [models.UniqueConstraint(fields=["company"], name="one_attendance_setting_per_company")]


class AttendanceRecord(CompanyOwned):
    """One employee on one day. No record means nothing was noted for that day."""

    class Status(models.TextChoices):
        PRESENT = "PRESENT", "Present"
        LATE = "LATE", "Late"
        ABSENT = "ABSENT", "Absent (unpaid)"
        HALF_DAY = "HALF_DAY", "Absent for half a day (unpaid)"

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="attendance")
    date = models.DateField()
    status = models.CharField(max_length=10, choices=Status.choices)
    time_in = models.TimeField(null=True, blank=True)
    time_out = models.TimeField(null=True, blank=True)
    note = models.CharField(max_length=200, blank=True)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name="+")

    class Meta:
        unique_together = [("employee", "date")]
        ordering = ["date", "employee_id"]

    def __str__(self):
        return f"{self.employee} {self.date:%d %b %Y}: {self.get_status_display()}"

    def clean(self):
        if self.time_in and self.time_out and self.time_out <= self.time_in:
            raise ValidationError({"time_out": "The time out must be after the time in."})

    @property
    def hours(self):
        if not (self.time_in and self.time_out):
            return None
        seconds = ((self.time_out.hour * 3600 + self.time_out.minute * 60 + self.time_out.second)
                   - (self.time_in.hour * 3600 + self.time_in.minute * 60 + self.time_in.second))
        return (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"))


class OvertimeRequest(CompanyOwned):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Waiting for approval"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"
        CANCELLED = "CANCELLED", "Cancelled"

    class DayType(models.TextChoices):
        WEEKDAY = "WEEKDAY", "Weekday"
        WEEKEND = "WEEKEND", "Weekend"
        HOLIDAY = "HOLIDAY", "Public holiday"

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="overtime")
    date = models.DateField(help_text="The day the overtime was worked.")
    hours = models.DecimalField(max_digits=4, decimal_places=2)
    reason = models.CharField(max_length=300, blank=True)
    day_type = models.CharField(max_length=8, choices=DayType.choices, default=DayType.WEEKDAY)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    # fixed when the request is approved, so a later change of settings or salary cannot alter what was agreed
    hourly_rate = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    multiplier = models.DecimalField(max_digits=4, decimal_places=2, null=True, blank=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name="+")
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-id"]

    def __str__(self):
        return f"{self.employee}: {self.hours} h on {self.date:%d %b %Y}"


class OvertimeApplication(models.Model):
    """Records that a payslip paid this overtime, so it is never paid twice."""
    request = models.ForeignKey(OvertimeRequest, on_delete=models.CASCADE, related_name="applications")
    payslip = models.ForeignKey(Payslip, on_delete=models.CASCADE, related_name="+")
    amount = models.DecimalField(max_digits=14, decimal_places=2)
