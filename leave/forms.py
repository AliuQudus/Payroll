from datetime import date

from django import forms

from payroll.models import Employee

from . import services
from .models import LeaveEntitlement, LeaveRequest, LeaveType


class LeaveRequestForm(forms.ModelForm):
    class Meta:
        model = LeaveRequest
        fields = ["employee", "leave_type", "start_date", "end_date", "relief_officer", "reason"]
        labels = {"relief_officer": "Relief officer (optional)"}
        widgets = {"start_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
                   "end_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")}

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        staff = Employee.objects.filter(company=company, is_active=True).order_by("employee_no")
        self.fields["employee"].queryset = staff
        self.fields["relief_officer"].queryset = staff
        self.fields["leave_type"].queryset = LeaveType.objects.filter(company=company, is_active=True)
        self.days = None

    def clean(self):
        data = super().clean()
        employee, leave_type = data.get("employee"), data.get("leave_type")
        start, end = data.get("start_date"), data.get("end_date")
        if employee and leave_type and start and end:
            self.days, error = services.check_request(employee, leave_type, start, end)
            if error:
                raise forms.ValidationError(error)
            relief = data.get("relief_officer")
            if relief and relief == employee:
                self.add_error("relief_officer", "The relief officer cannot be the person going on leave.")
            elif relief and services.on_leave(relief, start, end):
                self.add_error("relief_officer", f"{relief} is on approved leave in that period.")
        return data


class EntitlementForm(forms.ModelForm):
    class Meta:
        model = LeaveEntitlement
        fields = ["employee", "leave_type", "year", "days"]

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["employee"].queryset = Employee.objects.filter(company=company).order_by("employee_no")
        self.fields["leave_type"].queryset = LeaveType.objects.filter(company=company)
        if not self.instance.pk:
            self.initial.setdefault("year", date.today().year)
