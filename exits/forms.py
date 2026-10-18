from django import forms

from payroll.models import Employee

from . import services
from .models import ClearanceItem, Exit, SettlementLine


def _date():
    return forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


class ExitForm(forms.ModelForm):
    class Meta:
        model = Exit
        fields = ["employee", "reason", "notice_date", "last_working_day", "notes"]
        widgets = {"notice_date": _date(), "last_working_day": _date(), "notes": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, company, **kwargs):
        super().__init__(*args, **kwargs)
        self.company = company
        if self.instance.pk:
            del self.fields["employee"]                       # fixed once the exit exists
        else:
            leaving = Exit.objects.filter(company=company, status=Exit.Status.OPEN).values("employee_id")
            self.fields["employee"].queryset = (Employee.objects.filter(company=company, is_active=True)
                                                .exclude(pk__in=leaving).order_by("employee_no"))

    def clean_last_working_day(self):
        day = self.cleaned_data["last_working_day"]
        earliest = services.first_open_month(self.company)
        if earliest and day < earliest:
            raise forms.ValidationError(
                f"Payroll up to {earliest:%B %Y} has already been approved, so the final pay cannot be added to "
                f"it. Choose a date from {earliest:%d %B %Y}, or pay the difference with an adjustment.")
        return day


class ItemForm(forms.ModelForm):
    class Meta:
        model = ClearanceItem
        fields = ["name"]
        labels = {"name": "New clearance item"}


class LineForm(forms.ModelForm):
    class Meta:
        model = SettlementLine
        fields = ["kind", "description", "amount", "taxable"]
        help_texts = {"description": "For example: Pay in lieu of notice, Gratuity, Notice not served."}
