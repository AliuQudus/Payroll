from django import forms

from .models import PayGrade, SalaryRevision


class RevisionForm(forms.ModelForm):
    class Meta:
        model = SalaryRevision
        fields = ["effective_from", "gross_monthly", "reason", "grade", "note"]
        widgets = {"effective_from": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")}

    def __init__(self, *args, employee, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.employee = employee
        self.fields["reason"].choices = [c for c in SalaryRevision.Reason.choices
                                         if c[0] != SalaryRevision.Reason.OPENING]
        self.fields["grade"].queryset = PayGrade.objects.filter(company=employee.company)
        self.fields["effective_from"].help_text = ("Applies to the whole payroll month this date falls in. "
                                                   "A date in a month already approved adds back pay to the next run.")

    def clean_effective_from(self):
        return self.cleaned_data["effective_from"].replace(day=1)

    def clean_gross_monthly(self):
        gross = self.cleaned_data["gross_monthly"]
        if gross <= 0:
            raise forms.ValidationError("The salary must be above zero.")
        return gross

    def clean(self):
        data = super().clean()
        when = data.get("effective_from")
        if when:
            existing = SalaryRevision.objects.filter(employee=self.employee)
            if existing.filter(effective_from=when).exists():
                self.add_error("effective_from", "This employee already has a change from that month.")
            first = existing.order_by("effective_from").first()
            if first and when < first.effective_from:
                self.add_error("effective_from", "That is before this employee's salary history begins "
                                                 f"({first.effective_from:%b %Y}).")
        return data


class GradeForm(forms.ModelForm):
    class Meta:
        model = PayGrade
        fields = ["name", "min_gross", "max_gross"]
