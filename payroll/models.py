from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Sum

from .fields import EncryptedCharField


# Statutory rules entered as whole-number percentages (8 means 8%)
PERCENT_RULES = {"PENSION_EMPLOYEE", "PENSION_EMPLOYER", "ITF", "RENT_RELIEF_RATE", "NHF", "NSITF", "LEAVE_ALLOWANCE"}


class Company(models.Model):
    name = models.CharField(max_length=200)
    tax_id = models.CharField(max_length=50, blank=True)
    email = models.EmailField("Sender email", blank=True)
    smtp_host = models.CharField("Mail server", max_length=200, blank=True, default="smtp.gmail.com")
    smtp_port = models.PositiveIntegerField("Mail server port", default=587)
    smtp_username = models.CharField("Mail username", max_length=200, blank=True)
    smtp_password = EncryptedCharField("Mail password", blank=True)
    smtp_use_tls = models.BooleanField("Use TLS", default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class CompanyOwned(models.Model):
    """Every business table inherits this so data is isolated per company."""
    company = models.ForeignKey(Company, on_delete=models.PROTECT)

    class Meta:
        abstract = True


class Employee(CompanyOwned):
    employee_no = models.CharField(max_length=30)
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    email = models.EmailField(blank=True)
    department = models.CharField(max_length=100, blank=True)
    bank_name = models.CharField(max_length=100, blank=True)
    account_number = models.CharField(max_length=20, blank=True)
    gross_monthly = models.DecimalField(max_digits=14, decimal_places=2)
    annual_rent = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    pension_applies = models.BooleanField(default=True)
    nhf_applies = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = [("company", "employee_no")]

    def __str__(self):
        return f"{self.employee_no} {self.first_name} {self.last_name}"


class SalaryElement(CompanyOwned):
    class Kind(models.TextChoices):
        EARNING = "EARNING", "Earning"
        DEDUCTION = "DEDUCTION", "Deduction"
        EMPLOYER = "EMPLOYER", "Employer contribution"

    code = models.CharField(max_length=30)
    name = models.CharField(max_length=100)
    kind = models.CharField(max_length=10, choices=Kind.choices)
    formula = models.CharField(
        max_length=300,
        help_text="e.g. GROSS * 0.4, BASIC * 0.1, MIN(BASIC * 0.2, 500000), 50000",
    )
    is_taxable = models.BooleanField(default=False)
    is_pensionable = models.BooleanField(default=False)
    in_nhf_base = models.BooleanField(default=False)
    in_leave_base = models.BooleanField(
        "leave allowance base", default=False,
        help_text="Counts toward the annual pay the leave allowance is calculated on, for example Basic.")
    monthly_cap = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = [("company", "code")]

    def __str__(self):
        return f"{self.code} - {self.name}"


class EmployeeElementOverride(models.Model):
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="overrides")
    element = models.ForeignKey(SalaryElement, on_delete=models.CASCADE)
    formula = models.CharField(max_length=300)

    class Meta:
        unique_together = [("employee", "element")]


class TaxBand(CompanyOwned):
    effective_from = models.DateField()
    lower_limit = models.DecimalField(max_digits=16, decimal_places=2)
    upper_limit = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    rate = models.DecimalField("rate (%)", max_digits=6, decimal_places=4)

    class Meta:
        ordering = ["effective_from", "lower_limit"]


class StatutoryRule(CompanyOwned):
    class Code(models.TextChoices):
        PENSION_EMPLOYEE = "PENSION_EMPLOYEE", "Pension rate (employee)"
        PENSION_EMPLOYER = "PENSION_EMPLOYER", "Pension rate (employer)"
        RENT_RELIEF_RATE = "RENT_RELIEF_RATE", "Rent relief rate"
        RENT_RELIEF_CAP = "RENT_RELIEF_CAP", "Rent relief annual cap"
        ABSENCE_DIVISOR = "ABSENCE_DIVISOR", "Days used to value one day of absence (e.g. 30)"
        LEAVE_ALLOWANCE = "LEAVE_ALLOWANCE", "Leave allowance (% of annual pay)"

    code = models.CharField(max_length=30, choices=Code.choices)
    value = models.DecimalField(max_digits=16, decimal_places=4)
    effective_from = models.DateField()

    class Meta:
        unique_together = [("company", "code", "effective_from")]

    def clean(self):
        if self.code in PERCENT_RULES and self.value is not None and not 0 <= self.value <= 100:
            raise ValidationError({"value": "Enter a percentage between 0 and 100, for example 8 for 8%."})

    @property
    def shown_value(self):
        v = self.value
        if self.code in PERCENT_RULES:
            return f"{v.normalize():f}%"
        if self.code == "RENT_RELIEF_CAP":
            return f"\u20a6{v:,.2f}"
        return f"{v.normalize():f} days"


class PayrollRun(CompanyOwned):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        PENDING = "PENDING", "Pending approval"
        APPROVED = "APPROVED", "Approved"
        PAID = "PAID", "Paid"

    year = models.PositiveSmallIntegerField()
    month = models.PositiveSmallIntegerField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+", null=True
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+", null=True, blank=True
    )
    approved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = [("company", "year", "month")]


class Payslip(models.Model):
    """Locked snapshot once the run is approved."""
    run = models.ForeignKey(PayrollRun, on_delete=models.CASCADE, related_name="payslips")
    employee = models.ForeignKey(Employee, on_delete=models.PROTECT)
    gross = models.DecimalField(max_digits=14, decimal_places=2)
    total_earnings = models.DecimalField(max_digits=14, decimal_places=2)
    total_deductions = models.DecimalField(max_digits=14, decimal_places=2)
    paye = models.DecimalField(max_digits=14, decimal_places=2)
    net_pay = models.DecimalField(max_digits=14, decimal_places=2)
    employer_cost = models.DecimalField(max_digits=14, decimal_places=2)
    emailed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = [("run", "employee")]


class PayslipLine(models.Model):
    payslip = models.ForeignKey(Payslip, on_delete=models.CASCADE, related_name="lines")
    code = models.CharField(max_length=30)
    name = models.CharField(max_length=100)
    kind = models.CharField(max_length=10)
    amount = models.DecimalField(max_digits=14, decimal_places=2)


class EmployeeAdjustment(CompanyOwned):
    """Loans, absences, IOUs, other additions and other deductions, one row each per employee."""

    class Kind(models.TextChoices):
        LOAN = "LOAN", "Loan"
        ABSENCE = "ABSENCE", "Absence"
        IOU = "IOU", "IOU"
        ADDITION = "ADDITION", "Other addition"
        DEDUCTION = "DEDUCTION", "Other deduction"

    kind = models.CharField(max_length=10, choices=Kind.choices)
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="adjustments")
    description = models.CharField(max_length=200)
    start_year = models.PositiveSmallIntegerField("year")
    start_month = models.PositiveSmallIntegerField("month")
    amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    months = models.PositiveSmallIntegerField(default=1)
    days = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    is_taxable = models.BooleanField("taxable", default=True)
    is_active = models.BooleanField("active", default=True)
    is_treated = models.BooleanField("treated", default=False, editable=False)   # set by the system only
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-start_year", "-start_month", "employee_id"]

    def __str__(self):
        return f"{self.get_kind_display()}: {self.employee} - {self.description}"

    @property
    def period(self):
        return f"{self.start_year}-{self.start_month:02d}"

    def paid_before(self, run=None):
        """Loan repayments recorded in approved or paid runs (optionally excluding one run)."""
        qs = self.applications.filter(payslip__run__status__in=["APPROVED", "PAID"])
        if run is not None:
            qs = qs.exclude(payslip__run=run)
        return qs.aggregate(total=Sum("amount"))["total"] or Decimal("0")

    @property
    def total_expected(self):
        if self.kind == "ABSENCE":
            return None
        if self.kind in ("ADDITION", "DEDUCTION"):
            return self.amount * max(self.months, 1)
        return self.amount

    @property
    def applied(self):
        """Amount taken (or paid out) so far, counting approved and paid runs only."""
        return self.paid_before()

    @property
    def remaining(self):
        total = self.total_expected
        return None if total is None else max(total - self.applied, Decimal("0"))

    @property
    def per_installment(self):
        if self.kind in ("LOAN", "IOU"):
            return (self.amount / max(self.months, 1)).quantize(Decimal("0.01"), ROUND_HALF_UP)
        return self.amount if self.kind in ("ADDITION", "DEDUCTION") else None

    @property
    def progress(self):
        if self.kind == "ABSENCE":
            return ""
        done = self.applications.filter(payslip__run__status__in=["APPROVED", "PAID"]).count()
        return f"{done} of {self.months}"

    @property
    def status(self):
        return "Treated" if self.is_treated else ("Paused" if not self.is_active else "Open")

    def is_complete(self):
        if self.kind == "ABSENCE":
            return self.applied > 0
        return self.applied >= self.total_expected


class AdjustmentApplication(models.Model):
    """Records what a loan deducted on a payslip, so the balance can be tracked."""
    adjustment = models.ForeignKey(EmployeeAdjustment, on_delete=models.PROTECT, related_name="applications")
    payslip = models.ForeignKey(Payslip, on_delete=models.CASCADE, related_name="applications")
    amount = models.DecimalField(max_digits=14, decimal_places=2)


class CompanyAccess(models.Model):
    """Which companies a standard user may work in. Administrators can work in all of them."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="company_access")
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="access")

    class Meta:
        unique_together = [("user", "company")]


class Approver(CompanyOwned):
    """A user designated to approve one kind of thing in one company."""

    class Scope(models.TextChoices):
        PAYROLL = "PAYROLL", "Payroll runs"
        LEAVE = "LEAVE", "Leave requests"
        OVERTIME = "OVERTIME", "Overtime requests"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="approver_roles")
    scope = models.CharField(max_length=10, choices=Scope.choices)

    class Meta:
        unique_together = [("company", "user", "scope")]
        ordering = ["scope", "user_id"]

    def __str__(self):
        return f"{self.user} - {self.get_scope_display()}"


class UserPreference(models.Model):
    """How a person likes the site to look. Saved on the account, so it follows them to any device."""

    class Theme(models.TextChoices):
        LIGHT = "light", "Light"
        DARK = "dark", "Dark"
        SYSTEM = "system", "Match my device"

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="preference")
    theme = models.CharField(max_length=6, choices=Theme.choices, default=Theme.SYSTEM)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user}: {self.theme}"
