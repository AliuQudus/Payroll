from datetime import date

from django import forms

from .models import Authority, RemittancePayment


class PaymentForm(forms.ModelForm):
    class Meta:
        model = RemittancePayment
        fields = ["paid_date", "amount", "payee", "reference", "note"]
        widgets = {"paid_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")}

    def __init__(self, *args, run, authority, outstanding, **kwargs):
        super().__init__(*args, **kwargs)
        self.run, self.outstanding = run, outstanding
        self.fields["amount"].help_text = f"Still to pay: ₦{outstanding:,.2f}. Pay in parts if you need to."
        if authority == Authority.PENSION:
            self.fields["payee"].help_text = "The pension administrator this payment went to."
        if not self.is_bound:
            self.initial.setdefault("paid_date", date.today())
            self.initial.setdefault("amount", outstanding)

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        if amount <= 0:
            raise forms.ValidationError("Enter an amount above zero.")
        if amount > self.outstanding:
            raise forms.ValidationError(f"That is more than the ₦{self.outstanding:,.2f} still outstanding.")
        return amount

    def clean_paid_date(self):
        paid = self.cleaned_data["paid_date"]
        if paid > date.today():
            raise forms.ValidationError("A payment cannot be dated in the future.")
        if paid < date(self.run.year, self.run.month, 1):
            raise forms.ValidationError("A payment cannot be dated before the payroll month.")
        return paid


class DeadlineForm(forms.Form):
    paye_day = forms.IntegerField(label="PAYE: day of the following month", min_value=1, max_value=28)
    pension_day = forms.IntegerField(label="Pension: day of the following month", min_value=1, max_value=28)
