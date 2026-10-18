import os
import shutil
import tempfile
from datetime import date
from decimal import Decimal as D
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from payroll.models import Company, CompanyAccess, Employee

from . import services
from .forms import ProfileForm
from .models import EmergencyContact, EmployeeDocument, EmployeeProfile

TODAY = date(2026, 10, 9)
MEDIA = tempfile.mkdtemp(prefix="hr-test-media-")


def make_employee(company, no, first="Ada", last="Obi", **kw):
    return Employee.objects.create(company=company, employee_no=no, first_name=first, last_name=last,
                                   gross_monthly=D("300000"), **kw)


def make_profile(emp, **kw):
    kw.setdefault("job_title", "Accountant")
    kw.setdefault("hire_date", date(2022, 3, 1))
    return EmployeeProfile.objects.create(employee=emp, **kw)


def pdf(name="id.pdf"):
    return SimpleUploadedFile(name, b"%PDF-1.4 sample", content_type="application/pdf")


@override_settings(MEDIA_ROOT=MEDIA)
class HrTestCase(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def setUp(self):
        self.co = Company.objects.create(name="Alpha Ltd")
        self.other = Company.objects.create(name="Beta Ltd")
        self.ada = make_employee(self.co, "E001", "Ada", "Obi", department="Finance")
        self.tunde = make_employee(self.co, "E002", "Tunde", "Bello")
        self.stranger = make_employee(self.other, "E001", "Zed", "Zulu")
        users = get_user_model().objects
        self.admin = users.create_superuser("admin", password="x")
        self.staff = users.create_user("staff")                         # standard user, company Alpha only
        CompanyAccess.objects.create(user=self.staff, company=self.co)
        self.client = Client()
        self.client.force_login(self.admin)


class ProfileRulesTests(HrTestCase):
    def check(self, **kw):
        profile = EmployeeProfile(employee=kw.pop("employee", self.ada), job_title="X",
                                  hire_date=kw.pop("hire_date", date(2022, 3, 1)), **kw)
        profile.full_clean()
        return profile

    def test_valid_profile_passes(self):
        self.check(probation_end_date=date(2022, 9, 1), date_of_birth=date(1990, 5, 5))

    def test_dates_must_follow_hire_date(self):
        for field in ("probation_end_date", "contract_end_date"):
            with self.assertRaises(ValidationError) as ctx:
                self.check(**{field: date(2022, 1, 1)})
            self.assertIn(field, ctx.exception.message_dict)

    def test_date_of_birth_rules(self):
        with self.assertRaises(ValidationError):
            self.check(date_of_birth=date(2030, 1, 1))
        with self.assertRaises(ValidationError):
            self.check(date_of_birth=date(2022, 3, 1))           # born on the hire date

    def test_cannot_manage_yourself(self):
        with self.assertRaises(ValidationError) as ctx:
            self.check(manager=self.ada)
        self.assertIn("manager", ctx.exception.message_dict)

    def test_manager_must_be_in_the_same_company(self):
        with self.assertRaises(ValidationError):
            self.check(manager=self.stranger)

    def test_reporting_loop_is_refused(self):
        make_profile(self.tunde, manager=self.ada)               # Tunde reports to Ada
        with self.assertRaises(ValidationError) as ctx:
            self.check(manager=self.tunde)                       # Ada reporting to Tunde closes the loop
        self.assertIn("loop", str(ctx.exception))

    def test_form_only_offers_active_colleagues_as_managers(self):
        gone = make_employee(self.co, "E003", "Old", "Hand", is_active=False)
        form = ProfileForm(instance=EmployeeProfile(employee=self.ada), company=self.co)
        offered = set(form.fields["manager"].queryset)
        self.assertEqual(offered, {self.tunde})
        self.assertNotIn(gone, offered)


class ServiceTests(HrTestCase):
    def test_service_text(self):
        t = TODAY
        self.assertEqual(services.service_text(date(2023, 8, 9), t), "3 yrs 2 mos")
        self.assertEqual(services.service_text(date(2025, 10, 9), t), "1 yr")
        self.assertEqual(services.service_text(date(2026, 9, 10), t), "Less than a month")   # day not reached yet
        self.assertEqual(services.service_text(date(2026, 8, 9), t), "2 mos")
        self.assertTrue(services.service_text(date(2027, 1, 4), t).startswith("Starts"))

    def test_next_occurrence_handles_year_end_and_leap_day(self):
        self.assertEqual(services.next_occurrence(date(1990, 10, 9), TODAY), TODAY)        # today counts
        self.assertEqual(services.next_occurrence(date(1990, 1, 5), TODAY), date(2027, 1, 5))
        self.assertEqual(services.next_occurrence(date(1992, 2, 29), TODAY), date(2027, 2, 28))

    def test_alerts_pick_the_right_people(self):
        make_profile(self.ada, hire_date=date(2021, 10, 20), date_of_birth=date(1990, 10, 12),
                     contract_end_date=date(2026, 10, 1), probation_end_date=date(2026, 10, 15))
        make_profile(self.tunde, hire_date=date(2026, 6, 1), contract_end_date=date(2027, 6, 1))
        late = make_employee(self.co, "E003", "Late", "Comer")                              # no record
        EmployeeDocument.objects.create(employee=self.ada, title="Passport", expiry_date=date(2026, 10, 20),
                                        file="x.pdf")
        EmployeeDocument.objects.create(employee=self.tunde, title="Far away", expiry_date=date(2027, 12, 1),
                                        file="y.pdf")
        # people who left are never listed
        gone = make_employee(self.co, "E004", "Gone", "Away", is_active=False)
        make_profile(gone, contract_end_date=date(2026, 10, 2))
        EmployeeDocument.objects.create(employee=gone, title="Old", expiry_date=date(2026, 10, 2), file="z.pdf")

        data = services.alerts(self.co, today=TODAY, days=30)
        self.assertEqual([(d.title, left) for d, left in data["documents"]], [("Passport", 11)])
        self.assertEqual([(p.employee, left) for p, left in data["contracts"]], [(self.ada, -8)])
        self.assertEqual([(p.employee, left) for p, left in data["probation"]], [(self.ada, 6)])
        self.assertEqual([p.employee for p, *_ in data["birthdays"]], [self.ada])
        self.assertEqual([(p.employee, yrs) for p, _, yrs in data["anniversaries"]], [(self.ada, 5)])
        self.assertEqual(data["missing"], [late])

    def test_alerts_do_not_leak_between_companies(self):
        make_profile(self.stranger, contract_end_date=date(2026, 10, 2))
        self.assertEqual(services.alerts(self.co, today=TODAY)["contracts"], [])


class ViewTests(HrTestCase):
    def test_login_required(self):
        anon = Client()
        for name, args in [("hr:directory", []), ("hr:alerts", []), ("hr:detail", [self.ada.pk])]:
            resp = anon.get(reverse(name, args=args))
            self.assertEqual(resp.status_code, 302)
            self.assertIn("login", resp["Location"])

    def test_menu_shows_hr(self):
        resp = self.client.get(reverse("hr:directory"))
        self.assertContains(resp, ">HR</a>")
        self.assertContains(resp, ">Alerts</a>")

    def test_directory_search_and_inactive_toggle(self):
        make_profile(self.ada, job_title="Finance Manager")
        make_employee(self.co, "E003", "Old", "Hand", is_active=False)
        url = reverse("hr:directory")
        self.assertContains(self.client.get(url), "Finance Manager")
        self.assertNotContains(self.client.get(url), "Old Hand")
        self.assertContains(self.client.get(url, {"show": "all"}), "Old Hand")
        self.assertContains(self.client.get(url, {"q": "manager"}), "Ada")
        self.assertNotContains(self.client.get(url, {"q": "manager"}), "Tunde")
        self.assertContains(self.client.get(url, {"q": "zzz"}), "No employees match")
        self.assertNotContains(self.client.get(url, {"show": "all"}), "Zulu")              # other company

    def test_create_and_edit_profile(self):
        url = reverse("hr:profile_edit", args=[self.ada.pk])
        resp = self.client.post(url, {"job_title": "Finance Manager", "employment_type": "PERMANENT",
                                      "hire_date": "2022-03-01", "manager": self.tunde.pk,
                                      "tax_id": "1234567890"})
        self.assertRedirects(resp, reverse("hr:detail", args=[self.ada.pk]))
        profile = EmployeeProfile.objects.get(employee=self.ada)
        self.assertEqual((profile.job_title, profile.manager, profile.tax_id),
                         ("Finance Manager", self.tunde, "1234567890"))
        self.client.post(url, {"job_title": "Director", "employment_type": "PERMANENT", "hire_date": "2022-03-01"})
        self.assertEqual(EmployeeProfile.objects.filter(employee=self.ada).count(), 1)
        self.assertEqual(EmployeeProfile.objects.get(employee=self.ada).job_title, "Director")
        page = self.client.get(reverse("hr:detail", args=[self.ada.pk]))
        self.assertContains(page, "Director")

    def test_invalid_profile_is_not_saved(self):
        resp = self.client.post(reverse("hr:profile_edit", args=[self.ada.pk]),
                                {"job_title": "X", "employment_type": "PERMANENT", "hire_date": "2022-03-01",
                                 "contract_end_date": "2020-01-01"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "cannot end before the hire date")
        self.assertFalse(EmployeeProfile.objects.exists())

    def test_detail_without_record_offers_to_create_one(self):
        self.assertContains(self.client.get(reverse("hr:detail", args=[self.ada.pk])), "no HR record yet")

    def test_contacts_add_edit_delete(self):
        self.client.post(reverse("hr:contact_new", args=[self.ada.pk]),
                         {"name": "Chi Obi", "relationship": "Spouse", "phone": "0801", "is_next_of_kin": "on"})
        contact = EmergencyContact.objects.get()
        self.assertTrue(contact.is_next_of_kin)
        self.client.post(reverse("hr:contact_edit", args=[contact.pk]),
                         {"name": "Chi Obi", "relationship": "Wife", "phone": "0801"})
        contact.refresh_from_db()
        self.assertEqual((contact.relationship, contact.is_next_of_kin), ("Wife", False))
        self.assertEqual(self.client.get(reverse("hr:contact_delete", args=[contact.pk])).status_code, 405)
        self.client.post(reverse("hr:contact_delete", args=[contact.pk]))
        self.assertFalse(EmergencyContact.objects.exists())

    def test_nothing_in_another_company_is_reachable(self):
        c = Client()
        c.force_login(self.staff)                                                          # Alpha only
        foreign = EmergencyContact.objects.create(employee=self.stranger, name="N", relationship="R", phone="1")
        doc = EmployeeDocument.objects.create(employee=self.stranger, title="Secret", file="s.pdf")
        for url in (reverse("hr:detail", args=[self.stranger.pk]),
                    reverse("hr:profile_edit", args=[self.stranger.pk]),
                    reverse("hr:contact_new", args=[self.stranger.pk]),
                    reverse("hr:contact_edit", args=[foreign.pk]),
                    reverse("hr:document_new", args=[self.stranger.pk]),
                    reverse("hr:document_download", args=[doc.pk])):
            self.assertEqual(c.get(url).status_code, 404, url)
        self.assertEqual(c.post(reverse("hr:contact_delete", args=[foreign.pk])).status_code, 404)
        self.assertEqual(c.post(reverse("hr:document_delete", args=[doc.pk])).status_code, 404)
        self.assertTrue(EmergencyContact.objects.filter(pk=foreign.pk).exists())

    def test_user_without_company_access_is_refused(self):
        nobody = get_user_model().objects.create_user("nobody")
        c = Client()
        c.force_login(nobody)
        self.assertEqual(c.get(reverse("hr:directory")).status_code, 403)

    def test_alerts_page_renders(self):
        make_profile(self.ada, contract_end_date=date(2020, 1, 1), hire_date=date(2019, 1, 1))
        resp = self.client.get(reverse("hr:alerts"), {"days": "60"})
        self.assertContains(resp, "Contracts ending or overdue")
        self.assertContains(resp, "still active")
        self.assertEqual(self.client.get(reverse("hr:alerts"), {"days": "abc"}).status_code, 200)


class DocumentTests(HrTestCase):
    def upload(self, upload, **extra):
        data = {"kind": "ID", "title": "Passport", "file": upload, **extra}
        return self.client.post(reverse("hr:document_new", args=[self.ada.pk]), data)

    def test_upload_download_and_delete(self):
        resp = self.upload(pdf("My Passport.pdf"), expiry_date="2030-01-01")
        self.assertRedirects(resp, reverse("hr:detail", args=[self.ada.pk]))
        doc = EmployeeDocument.objects.get()
        self.assertEqual((doc.original_name, doc.uploaded_by), ("My Passport.pdf", self.admin))
        path = doc.file.path
        self.assertTrue(os.path.exists(path))
        self.assertIn(f"hr_documents/{self.co.pk}/{self.ada.pk}/", doc.file.name)         # stored under a random name
        self.assertNotIn("Passport", doc.file.name)

        dl = self.client.get(reverse("hr:document_download", args=[doc.pk]))
        self.assertEqual(b"".join(dl.streaming_content), b"%PDF-1.4 sample")
        self.assertIn("attachment", dl["Content-Disposition"])
        self.assertIn("My Passport.pdf", dl["Content-Disposition"])

        self.client.post(reverse("hr:document_delete", args=[doc.pk]))
        self.assertFalse(EmployeeDocument.objects.exists())
        self.assertFalse(os.path.exists(path))                                              # file removed from disk

    def test_bad_uploads_are_refused(self):
        cases = [SimpleUploadedFile("run.exe", b"MZ"),
                 SimpleUploadedFile("noext", b"%PDF-1.4"),
                 SimpleUploadedFile("fake.pdf", b"<html>not a pdf</html>"),
                 SimpleUploadedFile("fake.png", b"%PDF-1.4")]
        for upload in cases:
            self.assertEqual(self.upload(upload).status_code, 200, upload.name)
        self.assertFalse(EmployeeDocument.objects.exists())

    def test_oversized_upload_is_refused(self):
        with mock.patch("hr.forms.MAX_UPLOAD_BYTES", 10):
            resp = self.upload(pdf())
        self.assertContains(resp, "larger than")
        self.assertFalse(EmployeeDocument.objects.exists())

    def test_expiry_cannot_precede_issue(self):
        resp = self.upload(pdf(), issue_date="2026-05-01", expiry_date="2026-01-01")
        self.assertContains(resp, "cannot be before the issue date")

    def test_missing_file_on_disk_is_handled(self):
        doc = EmployeeDocument.objects.create(employee=self.ada, title="Ghost", file="hr_documents/none.pdf")
        resp = self.client.get(reverse("hr:document_download", args=[doc.pk]))
        self.assertRedirects(resp, reverse("hr:detail", args=[self.ada.pk]))

    def test_seed_command_is_repeatable(self):
        call_command("seed_hr", verbosity=0)
        first = EmployeeProfile.objects.count()
        call_command("seed_hr", verbosity=0)
        self.assertEqual(EmployeeProfile.objects.count(), first)
        self.assertEqual(first, 3)                                                          # every active employee
        self.assertFalse(EmployeeProfile.objects.filter(employee=self.stranger, manager__isnull=False).exists())
