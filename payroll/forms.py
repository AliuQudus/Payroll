import re

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

from . import access
from .engine import Element, FormulaError, order_elements
from .models import Approver, Company, CompanyAccess, Employee, EmployeeAdjustment, PayrollRun, SalaryElement
from .services import to_engine


class ElementForm(forms.ModelForm):
    class Meta:
        model = SalaryElement
        fields = ["code", "name", "kind", "formula", "is_taxable", "is_pensionable",
                  "monthly_cap", "in_leave_base", "is_active"]

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.company = company

    def clean_code(self):
        code = self.cleaned_data["code"].strip().upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", code):
            raise forms.ValidationError("Use letters, digits and underscores, starting with a letter.")
        if code in ("GROSS", "LEAVEALLOW"):
            raise forms.ValidationError(f"{code} is reserved.")
        return code

    def clean(self):
        data = super().clean()
        if self.company and data.get("code") and data.get("formula"):
            others = [to_engine(o) for o in SalaryElement.objects
                      .filter(company=self.company, is_active=True).exclude(pk=self.instance.pk)]
            this = Element(data["code"], data.get("name", ""), data.get("kind", "EARNING"), data["formula"])
            try:
                order_elements(others + [this])
            except FormulaError as exc:
                raise forms.ValidationError(str(exc))
        return data


class CompanyForm(forms.ModelForm):
    smtp_password = forms.CharField(
        label="Mail password", required=False, widget=forms.PasswordInput(render_value=False),
        help_text="For Gmail use an App Password, not your normal password. Leave blank to keep the saved one.")

    class Meta:
        model = Company
        fields = ["name", "tax_id", "email", "smtp_host", "smtp_port", "smtp_username", "smtp_password", "smtp_use_tls"]
        help_texts = {
            "email": "Payslips are sent from this address.",
            "smtp_host": "For Gmail: smtp.gmail.com",
            "smtp_port": "587 with TLS, or 465 for SSL.",
            "smtp_username": "Leave blank to use the sender email.",
        }

    def clean_smtp_password(self):
        value = self.cleaned_data.get("smtp_password")
        return value or (self.instance.smtp_password if self.instance.pk else "")


class RunForm(forms.ModelForm):
    class Meta:
        model = PayrollRun
        fields = ["year", "month"]

    def clean_month(self):
        month = self.cleaned_data["month"]
        if not 1 <= month <= 12:
            raise forms.ValidationError("Month must be between 1 and 12.")
        return month


ADJUSTMENT_FIELDS = {
    "LOAN": (["employee", "description", "amount", "months", "start_year", "start_month", "is_active"],
             {"amount": "Loan amount", "months": "Number of monthly installments",
              "start_year": "First deduction year", "start_month": "First deduction month"}),
    "ABSENCE": (["employee", "description", "days", "start_year", "start_month", "is_active"],
                {"days": "Days absent", "start_year": "Year", "start_month": "Month"}),
    "IOU": (["employee", "description", "amount", "months", "start_year", "start_month", "is_active"],
            {"amount": "Total amount owed", "months": "Recover over (months)",
             "start_year": "First deduction year", "start_month": "First deduction month"}),
    "ADDITION": (["employee", "description", "amount", "is_taxable", "months", "start_year", "start_month", "is_active"],
                 {"amount": "Amount per month", "months": "Number of months",
                  "start_year": "First payment year", "start_month": "First payment month"}),
    "DEDUCTION": (["employee", "description", "amount", "months", "start_year", "start_month", "is_active"],
                  {"amount": "Amount per month", "months": "Number of months",
                   "start_year": "First deduction year", "start_month": "First deduction month"}),
}


class BaseAdjustmentForm(forms.ModelForm):
    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        if company:
            self.fields["employee"].queryset = Employee.objects.filter(company=company).order_by("employee_no")
        if not self.instance.pk:
            from datetime import date
            self.initial.setdefault("start_year", date.today().year)
            self.initial.setdefault("start_month", date.today().month)

    def clean(self):
        data = super().clean()
        kind = self.instance.kind
        if data.get("start_month") is not None and not 1 <= data["start_month"] <= 12:
            self.add_error("start_month", "Month must be between 1 and 12.")
        if data.get("months") is not None and data["months"] < 1:
            self.add_error("months", "Must be at least 1.")
        if "amount" in self.fields and not data.get("amount"):
            self.add_error("amount", "Enter an amount greater than zero.")
        if kind == "ABSENCE" and not (data.get("days") and 0 < data["days"] <= 31):
            self.add_error("days", "Enter between 0.5 and 31 days.")
        return data


HELP = {
    "LOAN": {"months": "For example, 20,000 over 2 installments deducts 10,000 a month."},
    "IOU": {"months": "The total is recovered in equal parts over this many months."},
}


def adjustment_form(kind):
    fields, labels = ADJUSTMENT_FIELDS[kind]
    return forms.modelform_factory(EmployeeAdjustment, form=BaseAdjustmentForm, fields=fields,
                                   labels=labels, help_texts=HELP.get(kind, {}))


class UserForm(forms.ModelForm):
    role = forms.ChoiceField(
        choices=[("standard", "Standard user"), ("admin", "Administrator")],
        help_text="Administrators work in every company and manage users, approvers and company settings.")
    companies = forms.ModelMultipleChoiceField(
        queryset=Company.objects.none(), required=False, widget=forms.CheckboxSelectMultiple,
        label="Companies this user can work in")
    password1 = forms.CharField(label="Password", required=False, widget=forms.PasswordInput(render_value=False))
    password2 = forms.CharField(label="Confirm password", required=False, widget=forms.PasswordInput(render_value=False))

    class Meta:
        model = get_user_model()
        fields = ["username", "first_name", "last_name", "email", "is_active"]
        labels = {"is_active": "Active (can log in)"}

    def __init__(self, *args, acting_user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.acting_user = acting_user
        self.fields["companies"].queryset = Company.objects.order_by("name")
        self.fields["email"].required = False
        if self.instance.pk:
            del self.fields["username"]                       # fixed once the user exists
            self.fields["password1"].help_text = "Leave blank to keep the current password."
            self.initial["role"] = "admin" if self.instance.is_superuser else "standard"
            self.initial["companies"] = list(self.instance.company_access.values_list("company_id", flat=True))
        else:
            self.fields["password1"].required = self.fields["password2"].required = True
            self.fields["is_active"].initial = True
        self.order_fields(["username", "first_name", "last_name", "email", "role", "companies", "is_active",
                           "password1", "password2"])

    def clean(self):
        data = super().clean()
        pw1, pw2 = data.get("password1"), data.get("password2")
        if pw1 or pw2:
            if pw1 != pw2:
                self.add_error("password2", "The two passwords do not match.")
            else:
                try:
                    validate_password(pw1, self.instance)
                except ValidationError as exc:
                    self.add_error("password1", exc)
        role = data.get("role")
        if role == "standard" and not data.get("companies"):
            self.add_error("companies", "Choose at least one company, or make this user an administrator.")
        if self.instance.pk and self.instance.is_superuser and (role != "admin" or not data.get("is_active")):
            if self.instance == self.acting_user:
                raise ValidationError("You cannot remove your own administrator access or deactivate your own account.")
            others = get_user_model().objects.filter(is_superuser=True, is_active=True).exclude(pk=self.instance.pk)
            if not others.exists():
                raise ValidationError("There must be at least one active administrator.")
        return data

    def save(self, commit=True):
        user = super().save(commit=False)
        user.is_superuser = user.is_staff = self.cleaned_data["role"] == "admin"
        if self.cleaned_data.get("password1"):
            user.set_password(self.cleaned_data["password1"])
        user.save()
        chosen = set(self.cleaned_data["companies"].values_list("pk", flat=True))
        CompanyAccess.objects.filter(user=user).exclude(company_id__in=chosen).delete()
        for company_id in chosen:
            CompanyAccess.objects.get_or_create(user=user, company_id=company_id)
        return user


class ApproverForm(forms.ModelForm):
    class Meta:
        model = Approver
        fields = ["user", "scope"]
        labels = {"scope": "Can approve"}

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["user"].queryset = access.users_with_access(company)
        self.fields["user"].label_from_instance = lambda u: f"{access.name_of(u)} ({u.username})"
