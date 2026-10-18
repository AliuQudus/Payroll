from django import forms

from payroll.models import Employee

from .models import AttendanceSetting


class SettingForm(forms.ModelForm):
    class Meta:
        model = AttendanceSetting
        fields = ["standard_daily_hours", "hours_per_month", "base_percent", "weekday_rate", "weekend_rate",
                  "holiday_rate"]


class OvertimeForm(forms.Form):
    employee = forms.ModelChoiceField(queryset=Employee.objects.none())
    date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
                           help_text="The day the overtime was worked.")
    hours = forms.DecimalField(max_digits=4, decimal_places=2, min_value=0.25,
                               help_text="Hours of overtime on that day, for example 2.5.")
    reason = forms.CharField(max_length=300, required=False)

    def __init__(self, *args, company, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["employee"].queryset = Employee.objects.filter(company=company, is_active=True) \
            .order_by("employee_no")


class DecisionForm(forms.Form):
    note = forms.CharField(max_length=300, required=False)


class ImportForm(forms.Form):
    file = forms.FileField(label="CSV file", help_text="Columns: employee_no, date, status, and optionally "
                           "time_in, time_out, note. Up to 1 MB.")

    def clean_file(self):
        upload = self.cleaned_data["file"]
        if upload.size > 1024 * 1024:
            raise forms.ValidationError("The file is larger than 1 MB. Split it into smaller files.")
        try:
            self.text = upload.read().decode("utf-8-sig")
        except UnicodeDecodeError:
            raise forms.ValidationError("The file is not plain text. Save it as CSV (UTF-8) and try again.")
        return upload
