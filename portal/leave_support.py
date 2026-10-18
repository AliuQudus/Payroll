"""Leave pages for the portal. Imported only when the leave module is installed."""
from django import forms

from leave import services
from leave.models import LeaveRequest, LeaveType


class LeaveForm(forms.ModelForm):
    class Meta:
        model = LeaveRequest
        fields = ["leave_type", "start_date", "end_date", "reason"]
        widgets = {"start_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
                   "end_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")}

    def __init__(self, *args, employee, **kwargs):
        super().__init__(*args, **kwargs)
        self.employee, self.days = employee, None
        self.fields["leave_type"].queryset = LeaveType.objects.filter(company=employee.company, is_active=True)

    def clean(self):
        data = super().clean()
        kind, start, end = data.get("leave_type"), data.get("start_date"), data.get("end_date")
        if kind and start and end:
            self.days, error = services.check_request(self.employee, kind, start, end)
            if error:
                raise forms.ValidationError(error)
        return data


def balances(employee, year):
    return [(lt, services.balance(employee, lt, year))
            for lt in LeaveType.objects.filter(company=employee.company, is_active=True)]


def requests_of(employee):
    return LeaveRequest.objects.filter(employee=employee).select_related("leave_type")
