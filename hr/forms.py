from django import forms

from payroll.models import Employee

from .models import EmergencyContact, EmployeeDocument, EmployeeProfile

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
# extension -> the bytes a genuine file of that kind starts with (None where there is no reliable signature)
ALLOWED_FILES = {
    ".pdf": (b"%PDF",), ".png": (b"\x89PNG",), ".jpg": (b"\xff\xd8\xff",), ".jpeg": (b"\xff\xd8\xff",),
    ".doc": None, ".docx": (b"PK",),
}


def _date(**attrs):
    return forms.DateInput(attrs={"type": "date", **attrs}, format="%Y-%m-%d")


class ProfileForm(forms.ModelForm):
    class Meta:
        model = EmployeeProfile
        exclude = ["employee"]
        widgets = {f: _date() for f in ("hire_date", "probation_end_date", "contract_end_date", "date_of_birth")}
        help_texts = {
            "probation_end_date": "Leave blank if there is no probation.",
            "contract_end_date": "For fixed-term contracts and internships. Leave blank for open-ended jobs.",
            "manager": "Who this person reports to.",
        }

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        people = Employee.objects.filter(company=company, is_active=True).order_by("employee_no")
        if self.instance.employee_id:
            people = people.exclude(pk=self.instance.employee_id)
        self.fields["manager"].queryset = people


class ContactForm(forms.ModelForm):
    class Meta:
        model = EmergencyContact
        fields = ["name", "relationship", "phone", "address", "is_next_of_kin"]


class DocumentForm(forms.ModelForm):
    class Meta:
        model = EmployeeDocument
        fields = ["kind", "title", "file", "issue_date", "expiry_date"]
        widgets = {"issue_date": _date(), "expiry_date": _date()}
        help_texts = {
            "file": "PDF, JPG, PNG, DOC or DOCX, up to 10 MB.",
            "expiry_date": "Optional. Documents with an expiry date show up on the Alerts page before they lapse.",
        }

    def clean_file(self):
        upload = self.cleaned_data["file"]
        name = upload.name.lower()
        ext = name[name.rfind("."):] if "." in name else ""
        if ext not in ALLOWED_FILES:
            raise forms.ValidationError("Upload a PDF, JPG, PNG, DOC or DOCX file.")
        if upload.size > MAX_UPLOAD_BYTES:
            raise forms.ValidationError(f"The file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
        signatures = ALLOWED_FILES[ext]
        if signatures:
            head = upload.read(8)
            upload.seek(0)
            if not head.startswith(signatures):
                raise forms.ValidationError(f"This does not look like a real {ext[1:].upper()} file.")
        return upload

    def clean(self):
        data = super().clean()
        issued, expires = data.get("issue_date"), data.get("expiry_date")
        if issued and expires and expires < issued:
            self.add_error("expiry_date", "The expiry date cannot be before the issue date.")
        return data

    def save(self, commit=True):
        doc = super().save(commit=False)
        doc.original_name = self.cleaned_data["file"].name[:200]
        if commit:
            doc.save()
        return doc
