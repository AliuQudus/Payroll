"""Personal-details pages for the portal. Imported only when the HR module is installed."""
from django import forms

from hr.forms import ContactForm  # noqa: F401  (re-exported for the views)
from hr.models import EmergencyContact, EmployeeProfile


class DetailsForm(forms.ModelForm):
    """The only HR details an employee may change themselves. Job title, pay and dates stay with HR."""

    class Meta:
        model = EmployeeProfile
        fields = ["phone", "address"]


def profile_of(employee):
    return EmployeeProfile.objects.filter(employee=employee).select_related("manager").first()


def contacts_of(employee):
    return EmergencyContact.objects.filter(employee=employee)
