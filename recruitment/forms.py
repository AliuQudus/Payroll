from django import forms
from django.core.files.uploadedfile import UploadedFile

from .models import Applicant, Interview, Offer, OnboardingItem, Vacancy

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
# extension -> the bytes a genuine file of that kind starts with (None where there is no reliable signature)
ALLOWED_FILES = {".pdf": (b"%PDF",), ".doc": None, ".docx": (b"PK",)}


def _date():
    return forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


class VacancyForm(forms.ModelForm):
    class Meta:
        model = Vacancy
        fields = ["title", "department", "employment_type", "headcount", "description", "closing_date"]
        widgets = {"closing_date": _date(), "description": forms.Textarea(attrs={"rows": 5})}


class ApplicantForm(forms.ModelForm):
    class Meta:
        model = Applicant
        fields = ["first_name", "last_name", "email", "phone", "source", "notes", "cv"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}
        help_texts = {"cv": "PDF, DOC or DOCX, up to 10 MB."}

    def clean_cv(self):
        upload = self.cleaned_data.get("cv")
        if not isinstance(upload, UploadedFile):
            return upload                                          # nothing new was uploaded
        name = upload.name.lower()
        ext = name[name.rfind("."):] if "." in name else ""
        if ext not in ALLOWED_FILES:
            raise forms.ValidationError("Upload a PDF, DOC or DOCX file.")
        if upload.size > MAX_UPLOAD_BYTES:
            raise forms.ValidationError(f"The file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
        signatures = ALLOWED_FILES[ext]
        if signatures:
            head = upload.read(8)
            upload.seek(0)
            if not head.startswith(signatures):
                raise forms.ValidationError(f"This does not look like a real {ext[1:].upper()} file.")
        return upload

    def clean_email(self):
        email = self.cleaned_data["email"].strip()
        others = Applicant.objects.filter(vacancy=self.instance.vacancy, email__iexact=email).exclude(pk=self.instance.pk)
        if email and others.exists():
            raise forms.ValidationError("Someone with this email has already applied for this vacancy.")
        return email


class InterviewForm(forms.ModelForm):
    class Meta:
        model = Interview
        fields = ["when", "interviewer", "feedback", "rating"]
        widgets = {"when": forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
                   "feedback": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["when"].input_formats = ["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"]

    def clean_rating(self):
        rating = self.cleaned_data["rating"]
        if rating is not None and not 1 <= rating <= 5:
            raise forms.ValidationError("Rate from 1 to 5.")
        return rating


class OfferForm(forms.ModelForm):
    class Meta:
        model = Offer
        fields = ["job_title", "gross_monthly", "start_date", "employment_type", "probation_months", "notes"]
        widgets = {"start_date": _date()}


class HireForm(forms.Form):
    employee_no = forms.CharField(max_length=30, label="Employee number",
                                  help_text="Suggested from your existing numbers. Change it if you need to.")
    department = forms.CharField(max_length=100, required=False)


class ItemForm(forms.ModelForm):
    class Meta:
        model = OnboardingItem
        fields = ["name"]
        labels = {"name": "New item"}
