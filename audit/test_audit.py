import csv
import io
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from unittest import mock

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.checks import run_checks
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from hr.models import EmergencyContact, EmployeeProfile
from payroll.models import Company, CompanyAccess, Employee, Payslip, PayrollRun

from .models import AuditEntry
from .views import PAGE_SIZE


def entries(**filters):
    return list(AuditEntry.objects.filter(**filters).order_by("id"))


def pair(entry, word):
    """The [before, after] of the first changed field whose name contains `word`."""
    for field, values in entry.changes.items():
        if word.lower() in field.lower():
            return values
    raise AssertionError(f"No change containing {word!r} in {list(entry.changes)}")


class AuditCase(TestCase):
    def setUp(self):
        self.co = Company.objects.create(name="Alpha Ltd")
        self.other = Company.objects.create(name="Beta Ltd")
        self.ada = Employee.objects.create(company=self.co, employee_no="E001", first_name="Ada", last_name="Obi",
                                           gross_monthly=D("500000"), account_number="0123456789")
        users = get_user_model().objects
        self.admin = users.create_superuser("admin", password="pw-12345")
        self.staff = users.create_user("staff", password="pw-12345")
        CompanyAccess.objects.create(user=self.staff, company=self.co)
        self.baseline = AuditEntry.objects.count()
        self.client = Client()
        self.client.force_login(self.admin)

    def new_entries(self, **filters):
        return [e for e in entries(**filters) if e.pk > getattr(self, "_mark", 0)]

    def mark(self):
        last = AuditEntry.objects.order_by("-id").first()
        self._mark = last.pk if last else 0


class RecordingTests(AuditCase):
    def test_creating_a_record_is_logged_with_its_context(self):
        made = entries(model_label="payroll.employee", action="CREATE")[0]
        self.assertEqual((made.who, made.company_id, made.employee_id, made.subject),
                         ("System", self.co.pk, self.ada.pk, "E001 Ada Obi"))
        self.assertEqual(pair(made, "gross"), [None, "500000.00"])
        self.assertEqual(pair(made, "first name"), [None, "Ada"])
        self.assertNotIn("Middle", " ".join(made.changes))                       # empty fields are left out

    def test_a_change_shows_before_and_after(self):
        self.mark()
        self.ada.gross_monthly = D("600000")
        self.ada.is_active = False
        self.ada.save()
        (change,) = self.new_entries(model_label="payroll.employee")
        self.assertEqual(change.action, "UPDATE")
        self.assertEqual(pair(change, "gross"), ["500000.00", "600000.00"])
        self.assertEqual(pair(change, "active"), ["Yes", "No"])
        self.assertEqual(len(change.changes), 2)                                  # nothing else is listed

    def test_saving_without_changing_anything_records_nothing(self):
        self.mark()
        self.ada.save()
        self.ada.gross_monthly = "500000"                                         # same salary, different type
        self.ada.save()
        self.assertEqual(self.new_entries(), [])

    def test_only_the_fields_actually_saved_are_compared(self):
        self.mark()
        self.ada.first_name = "Adaeze"
        self.ada.gross_monthly = D("999999")                                      # changed in memory, not saved
        self.ada.save(update_fields=["first_name"])
        (change,) = self.new_entries(model_label="payroll.employee")
        self.assertEqual(list(change.changes), ["first name"])

    def test_deleting_is_logged_with_what_was_lost(self):
        pk = self.ada.pk
        self.mark()
        self.ada.delete()
        (gone,) = self.new_entries(model_label="payroll.employee")
        self.assertEqual((gone.action, gone.employee_id, gone.subject), ("DELETE", pk, "E001 Ada Obi"))
        self.assertEqual(pair(gone, "gross"), ["500000.00", None])

    def test_deleting_an_employee_with_records_does_not_break(self):
        EmergencyContact.objects.create(employee=self.ada, name="Chi", relationship="Wife", phone="1")
        EmployeeProfile.objects.create(employee=self.ada, job_title="X", hire_date=date(2020, 1, 1))
        self.ada.delete()                                                         # cascades; no dangling references
        self.assertTrue(entries(model_label="hr.emergencycontact", action="DELETE"))
        self.assertTrue(entries(model_label="hr.employeeprofile", action="DELETE"))

    def test_related_records_show_names_not_ids(self):
        boss = Employee.objects.create(company=self.co, employee_no="E000", first_name="Bola", last_name="Boss",
                                       gross_monthly=D("1"))
        profile = EmployeeProfile.objects.create(employee=self.ada, job_title="X", hire_date=date(2020, 1, 1))
        self.mark()
        profile.manager = boss
        profile.save()
        (change,) = self.new_entries(model_label="hr.employeeprofile")
        self.assertEqual(pair(change, "reports to"), [None, "E000 Bola Boss"])
        self.assertEqual((change.employee_id, change.company_id), (self.ada.pk, self.co.pk))

    def test_choice_fields_show_their_labels(self):
        run = PayrollRun.objects.create(company=self.co, year=2026, month=9)
        self.mark()
        run.status = "PENDING"
        run.save()
        (change,) = self.new_entries(model_label="payroll.payrollrun")
        self.assertEqual(pair(change, "status"), [PayrollRun.Status.DRAFT.label, PayrollRun.Status.PENDING.label])

    def test_payslips_are_not_logged_one_by_one(self):
        run = PayrollRun.objects.create(company=self.co, year=2026, month=9)
        self.mark()
        Payslip.objects.create(run=run, employee=self.ada, gross=1, total_earnings=1, total_deductions=0, paye=0,
                               net_pay=1, employer_cost=1)
        self.assertEqual(self.new_entries(model_label="payroll.payslip"), [])


class SecretsTests(AuditCase):
    def test_bank_account_and_mail_password_values_are_never_stored(self):
        self.mark()
        self.ada.account_number = "9998887776"
        self.ada.save()
        self.co.smtp_password = "hunter2-app-password"
        self.co.save()
        for e in self.new_entries():
            blob = json.dumps(e.changes)
            self.assertNotIn("9998887776", blob)
            self.assertNotIn("0123456789", blob)
            self.assertNotIn("hunter2", blob)
        change = self.new_entries(model_label="payroll.employee")[0]
        self.assertEqual(pair(change, "account"), ["(hidden)", "(hidden)"])      # but the change itself is visible
        self.assertEqual(pair(self.new_entries(model_label="payroll.company")[0], "password"), ["(hidden)", "(hidden)"])

    def test_a_password_change_is_logged_without_the_hash(self):
        old_hash = self.staff.password
        self.mark()
        self.staff.set_password("a-new-password-9")
        self.staff.save()
        (change,) = self.new_entries(model_label="auth.user")
        self.assertEqual(pair(change, "password"), ["(hidden)", "(hidden)"])
        self.assertNotIn(old_hash, json.dumps(change.changes))
        self.assertNotIn(self.staff.password, json.dumps(change.changes))

    def test_new_users_are_logged(self):
        created = {e.object_repr: e for e in entries(model_label="auth.user", action="CREATE")}
        self.assertEqual(set(created), {"admin", "staff"})
        self.assertEqual(pair(created["admin"], "superuser"), [None, "Yes"])
        self.assertEqual(pair(created["admin"], "password"), [None, "(hidden)"])

    def test_making_someone_an_administrator_is_logged(self):
        self.mark()
        self.staff.is_superuser = True
        self.staff.save()
        (change,) = self.new_entries(model_label="auth.user")
        self.assertEqual(pair(change, "superuser"), ["No", "Yes"])
        self.assertEqual(change.object_repr, "staff")

    def test_signing_in_alone_creates_no_user_change(self):
        self.mark()
        self.client.force_login(self.staff)                                       # updates last_login behind the scenes
        self.assertEqual(self.new_entries(model_label="auth.user"), [])


class WhoTests(AuditCase):
    def edit_gross(self, value):
        return self.client.post(reverse("section_edit", args=["employees", self.ada.pk]), {
            "employee_no": "E001", "first_name": "Ada", "last_name": "Obi", "gross_monthly": value,
            "annual_rent": "0", "pension_applies": "on", "is_active": "on", "account_number": "0123456789"})

    def test_a_change_through_the_site_names_the_person_and_address(self):
        self.mark()
        self.edit_gross("650000")
        (change,) = self.new_entries(model_label="payroll.employee")
        self.assertEqual((change.who, change.user, change.ip_address), ("admin", self.admin, "127.0.0.1"))
        self.assertEqual(pair(change, "gross"), ["500000.00", "650000.00"])

    def test_the_actor_does_not_leak_between_requests(self):
        self.edit_gross("650000")
        self.mark()
        self.ada.first_name = "Adaeze"                                            # later, outside any request
        self.ada.save()
        self.assertEqual(self.new_entries(model_label="payroll.employee")[0].who, "System")

    def test_sign_ins_and_failures_are_logged(self):
        self.mark()
        Client().post(reverse("login"), {"username": "staff", "password": "pw-12345"})
        Client().post(reverse("login"), {"username": "staff", "password": "wrong-guess"})
        Client().post(reverse("login"), {"username": "nobody", "password": "wrong-guess"})
        login, bad, unknown = self.new_entries()[-3:]
        self.assertEqual((login.action, login.who, login.user), ("LOGIN", "staff", self.staff))
        self.assertEqual((bad.action, bad.who, bad.user), ("LOGIN_FAILED", "staff", None))
        self.assertEqual(unknown.who, "nobody")
        self.assertNotIn("wrong-guess", json.dumps([e.changes for e in self.new_entries()]))
        self.assertNotIn("wrong-guess", " ".join(e.object_repr + e.username for e in self.new_entries()))

    def test_sensitive_downloads_are_logged(self):
        run = PayrollRun.objects.create(company=self.co, year=2026, month=9, status="APPROVED")
        Payslip.objects.create(run=run, employee=self.ada, gross=1, total_earnings=1, total_deductions=0, paye=0,
                               net_pay=1, employer_cost=1)
        self.mark()
        self.assertEqual(self.client.get(reverse("run_csv", args=[run.pk])).status_code, 200)
        (event,) = self.new_entries(action="EVENT")
        self.assertEqual((event.object_repr, event.who, event.company_id),
                         ("Bank payment file downloaded", "admin", self.co.pk))
        self.assertEqual(pair(event, "detail"), [None, f"pk={run.pk}"])

    def test_pages_that_are_not_watched_leave_no_event(self):
        self.mark()
        self.client.get(reverse("section_list", args=["employees"]))
        self.assertEqual(self.new_entries(action="EVENT"), [])


class SafetyTests(AuditCase):
    def test_a_fault_in_the_audit_log_never_stops_the_work(self):
        with mock.patch("audit.recorder.record_model", side_effect=RuntimeError("boom")):
            with self.assertLogs("audit.signals", level="ERROR") as logs:
                self.ada.first_name = "Adaeze"
                self.ada.save()
        self.ada.refresh_from_db()
        self.assertEqual(self.ada.first_name, "Adaeze")
        self.assertIn("could not record", logs.output[0])

    def test_entries_cannot_be_changed_or_deleted(self):
        entry = AuditEntry.objects.first()
        entry.object_repr = "tampered"
        with self.assertRaises(ValueError):
            entry.save()
        with self.assertRaises(ValueError):
            entry.delete()
        model_admin = admin.site._registry[AuditEntry]
        request = mock.Mock(user=self.admin)
        self.assertFalse(model_admin.has_add_permission(request))
        self.assertFalse(model_admin.has_change_permission(request, entry))
        self.assertFalse(model_admin.has_delete_permission(request, entry))

    def test_the_system_check_demands_the_middleware(self):
        self.assertNotIn("audit.E001", [e.id for e in run_checks()])
        without = [m for m in __import__("django.conf").conf.settings.MIDDLEWARE if "audit" not in m]
        with override_settings(MIDDLEWARE=without):
            self.assertIn("audit.E001", [e.id for e in run_checks()])


class ViewTests(AuditCase):
    def test_only_administrators_can_open_the_log(self):
        self.assertEqual(self.client.get(reverse("audit:log")).status_code, 200)
        staff = Client()
        staff.force_login(self.staff)
        for url in (reverse("audit:log"), reverse("audit:csv")):
            resp = staff.get(url)
            self.assertEqual(resp.status_code, 302)
            self.assertNotContains(staff.get(url, follow=True), "Audit log</h1>")
        anon = Client().get(reverse("audit:log"))
        self.assertIn("login", anon["Location"])

    def test_menu_and_employee_links_are_for_administrators(self):
        page = self.client.get(reverse("hr:detail", args=[self.ada.pk]))
        self.assertContains(page, ">Audit log</a>")
        self.assertContains(page, f"{reverse('audit:log')}?employee={self.ada.pk}")
        staff = Client()
        staff.force_login(self.staff)
        page = staff.get(reverse("hr:detail", args=[self.ada.pk]))
        self.assertNotContains(page, ">Audit log</a>")
        self.assertNotContains(page, "Audit trail")

    def test_log_shows_changes_and_filters(self):
        self.ada.gross_monthly = D("600000")
        self.ada.save()
        page = self.client.get(reverse("audit:log"))
        self.assertContains(page, "500000.00")
        self.assertContains(page, "600000.00")
        self.assertContains(page, "Ada Obi")
        only = self.client.get(reverse("audit:log"), {"action": "UPDATE", "q": "Ada"})
        self.assertContains(only, "600000.00")
        self.assertEqual({e.action for e in only.context["page"]}, {"UPDATE"})
        self.assertNotContains(self.client.get(reverse("audit:log"), {"q": "zzz-nothing"}), "Ada Obi")
        by_user = self.client.get(reverse("audit:log"), {"user": "nobody-here"})
        self.assertEqual(by_user.status_code, 200)                                # an unknown choice is rejected, not a crash
        self.assertContains(by_user, "Check the filters")

    def test_filter_by_employee_and_area(self):
        other = Employee.objects.create(company=self.co, employee_no="E002", first_name="Tunde", last_name="Bello",
                                        gross_monthly=D("1"))
        self.ada.first_name = "Adaeze"
        self.ada.save()
        page = self.client.get(reverse("audit:log"), {"employee": other.pk})
        self.assertContains(page, "Tunde Bello")
        self.assertNotContains(page, "Adaeze")
        area = self.client.get(reverse("audit:log"), {"area": "Employee", "q": "Tunde"})
        self.assertContains(area, "Tunde Bello")

    def test_date_filter(self):
        old = AuditEntry.objects.filter(model_label="payroll.employee").first()
        AuditEntry.objects.filter(pk=old.pk).update(created_at=datetime(2020, 1, 15, 9, 0, tzinfo=timezone.utc))
        in_range = self.client.get(reverse("audit:log"), {"date_from": "2020-01-01", "date_to": "2020-01-31"})
        self.assertContains(in_range, "15 Jan 2020")
        self.assertEqual([e.pk for e in in_range.context["page"]], [old.pk])
        recent = self.client.get(reverse("audit:log"), {"date_from": "2021-01-01"})
        self.assertNotContains(recent, "15 Jan 2020")

    def test_company_scope(self):
        theirs = Employee.objects.create(company=self.other, employee_no="E9", first_name="Zed", last_name="Zulu",
                                         gross_monthly=D("1"))
        page = self.client.get(reverse("audit:log"))
        self.assertNotContains(page, "Zed Zulu")
        self.assertContains(page, "Ada Obi")
        self.assertContains(page, "admin")                                        # users belong to no company: shown
        self.assertNotContains(self.client.get(reverse("audit:csv")), "Zed Zulu")

    def test_pagination_keeps_the_filters(self):
        AuditEntry.objects.bulk_create([AuditEntry(action="UPDATE", model_name="Employee", object_repr=f"Row {i}",
                                                   company_id=self.co.pk, username="admin")
                                        for i in range(PAGE_SIZE + 5)])
        first = self.client.get(reverse("audit:log"), {"user": "admin"})
        self.assertContains(first, "Page 1 of")
        self.assertContains(first, "user=admin&page=2")
        second = self.client.get(reverse("audit:log"), {"user": "admin", "page": 2})
        self.assertContains(second, "Page 2 of")
        self.assertEqual(self.client.get(reverse("audit:log"), {"page": "junk"}).status_code, 200)

    def test_csv_export(self):
        self.ada.gross_monthly = D("600000")
        self.ada.first_name = "=HYPERLINK(\"http://evil\")"
        self.ada.save()
        resp = self.client.get(reverse("audit:csv"), {"action": "UPDATE"})
        self.assertIn("attachment", resp["Content-Disposition"])
        rows = list(csv.reader(io.StringIO(resp.content.decode())))
        self.assertEqual(rows[0][:4], ["When", "Who", "IP address", "Action"])
        gross = [r for r in rows if r[7].lower().startswith("gross")]
        self.assertEqual((gross[0][3], gross[0][8], gross[0][9]), ("Changed", "500000.00", "600000.00"))
        self.assertTrue(any(r[9] == "'=HYPERLINK(\"http://evil\")" for r in rows))   # formulas defused
        self.assertEqual(AuditEntry.objects.filter(object_repr="Audit log exported").count(), 1)   # the export is itself logged
