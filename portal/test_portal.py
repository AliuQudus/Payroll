import re
import smtplib
from datetime import date
from decimal import Decimal as D
from unittest import mock, skipUnless

from django.apps import apps
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from hr.models import EmergencyContact, EmployeeProfile
from leave.models import LeaveRequest, LeaveType
from payroll.models import Company, CompanyAccess, Employee, Payslip, PayslipLine, PayrollRun

from . import services
from .models import EmployeeAccount

PASSWORD = "Str0ng-portal-pass-7531"


class FakeConnection:
    """Stands in for the company's mail server and keeps what would have been sent."""
    def __init__(self):
        self.sent = []

    def send_messages(self, messages):
        self.sent += messages
        return len(messages)

    def close(self):
        pass


def make_slip(run, employee, net, paye="1000.00"):
    slip = Payslip.objects.create(run=run, employee=employee, gross=net, total_earnings=net, total_deductions=0,
                                  paye=paye, net_pay=net, employer_cost=D(net) + 5000)
    PayslipLine.objects.create(payslip=slip, code="BASIC", name="Basic salary", kind="EARNING", amount=net)
    PayslipLine.objects.create(payslip=slip, code="PAYE", name="PAYE tax", kind="DEDUCTION", amount=paye)
    PayslipLine.objects.create(payslip=slip, code="PENSION_ER", name="Pension (employer)", kind="EMPLOYER", amount=2000)
    return slip


class PortalCase(TestCase):
    def setUp(self):
        self.co = Company.objects.create(name="Alpha Ltd", email="hr@alpha.test")
        self.other = Company.objects.create(name="Beta Ltd")
        mk = lambda co, no, first, last, email="", **kw: Employee.objects.create(
            company=co, employee_no=no, first_name=first, last_name=last, email=email, gross_monthly=D("500000"), **kw)
        self.ada = mk(self.co, "E001", "Ada", "Obi", "ada@a.test", account_number="0123456789", bank_name="GTBank")
        self.tunde = mk(self.co, "E002", "Tunde", "Bello", "tunde@a.test")
        self.cece = mk(self.co, "E003", "Cece", "Eze")                                   # no email
        self.zed = mk(self.other, "E001", "Zed", "Zulu", "zed@b.test")
        users = get_user_model().objects
        self.admin = users.create_superuser("admin", password="x")
        self.hr = users.create_user("hr-officer", password="x")
        CompanyAccess.objects.create(user=self.hr, company=self.co)
        # payslips: approved and paid are visible to the employee; draft and pending are not
        self.runs = {s: PayrollRun.objects.create(company=self.co, year=2026, month=m, status=s)
                     for m, s in ((8, "APPROVED"), (9, "PAID"), (10, "DRAFT"), (11, "PENDING"))}
        self.slips = {}
        for status, run in self.runs.items():
            self.slips[status] = make_slip(run, self.ada, "450000.00")
            make_slip(run, self.tunde, "123456.78")
        self.mail = FakeConnection()
        patcher = mock.patch("portal.services.open_mail_connection", return_value=self.mail)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.admin_client = Client()
        self.admin_client.force_login(self.admin)

    def login_as(self, employee):
        """Give the employee a login with a known password, signed in on a fresh client."""
        account = services.grant(employee, self.admin)
        account.user.set_password(PASSWORD)
        account.user.save()
        client = Client()
        self.assertTrue(client.login(username=account.user.get_username(), password=PASSWORD))
        return client, account


class AccessAdminTests(PortalCase):
    def grant(self, employee, client=None):
        return (client or self.admin_client).post(reverse("portal:access_grant", args=[employee.pk]), follow=True)

    def test_granting_creates_a_safe_login_and_emails_the_link(self):
        resp = self.grant(self.ada)
        self.assertContains(resp, "invitation has been emailed")
        account = EmployeeAccount.objects.get(employee=self.ada)
        user = account.user
        self.assertEqual((user.username, user.email), ("ada@a.test", "ada@a.test"))
        self.assertFalse(user.has_usable_password())                                      # no password exists yet
        self.assertEqual((user.is_staff, user.is_superuser, user.is_active), (False, False, True))
        self.assertFalse(CompanyAccess.objects.filter(user=user).exists())                # no company screens
        self.assertEqual(account.created_by, self.admin)
        self.assertIsNotNone(account.invited_at)
        (message,) = self.mail.sent
        self.assertEqual(message.to, ["ada@a.test"])
        self.assertIn("/accounts/reset/", message.body)
        self.assertIn("Alpha Ltd", message.subject)

    def test_the_emailed_link_sets_a_password_once_and_lands_in_the_portal(self):
        self.grant(self.ada)
        link = re.search(r"http://testserver\S+", self.mail.sent[0].body).group(0)
        client = Client()
        page = client.get(link, follow=True)
        self.assertTrue(page.context["validlink"])
        url = page.redirect_chain[-1][0]
        self.assertEqual(client.post(url, {"new_password1": PASSWORD, "new_password2": PASSWORD}).status_code, 302)
        self.assertTrue(client.login(username="ada@a.test", password=PASSWORD))
        self.assertRedirects(client.get("/"), reverse("portal:home"), fetch_redirect_response=False)
        self.assertContains(client.get(reverse("portal:home")), "Hello, Ada")
        self.assertFalse(Client().get(link, follow=True).context["validlink"])            # the link works only once

    def test_grant_refusals_create_nothing(self):
        self.assertContains(self.grant(self.cece), "Add an email address")
        get_user_model().objects.create_user("TUNDE@a.test")                              # username taken, any case
        self.assertContains(self.grant(self.tunde), "already exists")
        self.grant(self.ada)
        self.assertContains(self.grant(self.ada), "already has portal access")
        self.ada.is_active = False
        self.ada.save()
        gone = Employee.objects.create(company=self.co, employee_no="E9", first_name="Gone", last_name="Away",
                                       email="gone@a.test", gross_monthly=D("1"), is_active=False)
        self.assertContains(self.grant(gone), "has left")
        self.assertEqual(EmployeeAccount.objects.count(), 1)
        self.assertEqual(self.admin_client.post(reverse("portal:access_grant", args=[self.zed.pk])).status_code, 404)

    def test_when_email_is_not_set_up_the_link_is_shown_to_the_administrator(self):
        with mock.patch("portal.services.open_mail_connection", side_effect=ValueError("Set up the company email first")):
            resp = self.grant(self.ada)
        self.assertContains(resp, "not sent")
        self.assertContains(resp, "link yourself")
        self.assertContains(resp, "/accounts/reset/")
        account = EmployeeAccount.objects.get(employee=self.ada)
        self.assertIsNone(account.invited_at)

    def test_a_mail_server_failure_is_reported(self):
        failing = FakeConnection()
        failing.send_messages = mock.Mock(side_effect=smtplib.SMTPException("boom"))
        with mock.patch("portal.services.open_mail_connection", return_value=failing):
            resp = self.grant(self.ada)
        self.assertContains(resp, "not sent")
        self.assertIsNone(EmployeeAccount.objects.get().invited_at)

    def test_switching_off_stops_the_login_and_any_open_session(self):
        client, account = self.login_as(self.ada)
        self.assertEqual(client.get(reverse("portal:home")).status_code, 200)
        self.admin_client.post(reverse("portal:access_revoke", args=[self.ada.pk]))
        account.user.refresh_from_db()
        self.assertFalse(account.user.is_active)
        self.assertEqual(client.get(reverse("portal:home")).status_code, 302)             # session no longer works
        self.assertFalse(Client().login(username="ada@a.test", password=PASSWORD))
        self.assertEqual(services.status_of(Employee.objects.get(pk=self.ada.pk)), "revoked")

    def test_restoring_and_resending(self):
        _, account = self.login_as(self.ada)
        services.revoke(account)
        before = len(self.mail.sent)
        self.admin_client.post(reverse("portal:access_invite", args=[self.ada.pk]))
        account.user.refresh_from_db()
        self.assertTrue(account.user.is_active)
        self.assertEqual(len(self.mail.sent), before + 1)
        self.assertEqual(self.admin_client.post(reverse("portal:access_invite", args=[self.tunde.pk]),
                                                follow=True).status_code, 200)             # no login to invite: refused
        self.assertEqual(EmployeeAccount.objects.count(), 1)

    def test_access_page_shows_each_state(self):
        _, active = self.login_as(self.ada)                                              # signed in once: active
        invited = services.grant(self.tunde, self.admin)
        page = self.admin_client.get(reverse("portal:access"))
        statuses = {r["emp"].pk: r["status"] for r in page.context["rows"]}
        self.assertEqual(statuses, {self.ada.pk: "active", self.tunde.pk: "invited", self.cece.pk: "none"})
        services.revoke(invited)
        self.tunde.is_active = False
        self.tunde.save()
        self.assertEqual({r["emp"].pk: r["status"] for r in self.admin_client.get(
            reverse("portal:access")).context["rows"]}[self.tunde.pk], "left")
        CompanyAccess.objects.create(user=active.user, company=self.co)
        flagged = {r["emp"].pk: r["also_staff"] for r in self.admin_client.get(reverse("portal:access")).context["rows"]}
        self.assertTrue(flagged[self.ada.pk])

    def test_only_administrators_manage_access(self):
        staff = Client()
        staff.force_login(self.hr)
        for url in (reverse("portal:access"),):
            self.assertEqual(staff.get(url).status_code, 302)
        self.assertEqual(staff.post(reverse("portal:access_grant", args=[self.ada.pk])).status_code, 302)
        self.assertFalse(EmployeeAccount.objects.exists())
        menu = self.admin_client.get(reverse("portal:access"))
        self.assertContains(menu, ">Portal access</a>")


class IsolationTests(PortalCase):
    def setUp(self):
        super().setUp()
        self.client, self.account = self.login_as(self.ada)

    def test_every_portal_page_needs_a_login(self):
        urls = [reverse("portal:home"), reverse("portal:payslips"), reverse("portal:leave"),
                reverse("portal:details"), reverse("portal:payslip", args=[self.slips["PAID"].pk])]
        for url in urls:
            resp = Client().get(url)
            self.assertEqual(resp.status_code, 302, url)
            self.assertIn("login", resp["Location"])

    def test_a_login_with_no_employee_gets_nothing(self):
        staff = Client()
        staff.force_login(self.hr)
        resp = staff.get(reverse("portal:home"))
        self.assertEqual(resp.status_code, 403)
        self.assertContains(resp, "not linked to an employee", status_code=403)

    def test_the_payroll_screens_are_closed_to_an_employee_login(self):
        pages = [reverse("section_list", args=["employees"]), reverse("run_list"), reverse("payslip_list"),
                 reverse("payslip", args=[self.slips["PAID"].pk]), reverse("user_list"), reverse("calculator"),
                 reverse("hr:directory"), reverse("hr:detail", args=[self.tunde.pk]), reverse("salary:overview"),
                 reverse("leave:requests"), reverse("remittances:overview"), reverse("exits:overview"),
                 reverse("portal:access"), reverse("run_csv", args=[self.runs["PAID"].pk])]
        if apps.is_installed("audit"):
            pages.append(reverse("audit:log"))
        for url in pages:
            resp = self.client.get(url)
            self.assertIn(resp.status_code, (302, 403), url)
            self.assertNotContains(resp, "Tunde", status_code=resp.status_code)
        refused = self.client.post(reverse("portal:access_grant", args=[self.tunde.pk]))
        self.assertIn(refused.status_code, (302, 403))
        self.assertFalse(EmployeeAccount.objects.filter(employee=self.tunde).exists())     # and nothing was created

    def test_switching_company_cannot_unlock_the_payroll_screens(self):
        self.client.post(reverse("company_switch"), {"company": self.co.pk})
        self.assertIsNone(self.client.session.get("company_id"))
        self.assertEqual(self.client.get(reverse("section_list", args=["employees"])).status_code, 403)

    def test_landing_page_depends_on_who_signed_in(self):
        self.assertRedirects(self.client.get("/"), reverse("portal:home"), fetch_redirect_response=False)
        hr = Client()
        hr.force_login(self.hr)
        self.assertRedirects(hr.get("/"), reverse("section_list", args=["employees"]), fetch_redirect_response=False)
        self.assertRedirects(self.admin_client.get("/"), reverse("section_list", args=["employees"]),
                             fetch_redirect_response=False)

    def test_staff_who_are_also_employees_can_use_both(self):
        EmployeeAccount.objects.create(user=self.hr, employee=self.tunde)
        both = Client()
        both.force_login(self.hr)
        self.assertContains(both.get(reverse("portal:home")), "Back to payroll")
        self.assertRedirects(both.get("/"), reverse("section_list", args=["employees"]), fetch_redirect_response=False)
        self.assertEqual(both.get(reverse("section_list", args=["employees"])).status_code, 200)

    def test_an_employee_who_has_left_loses_access(self):
        self.ada.is_active = False
        self.ada.save()
        resp = self.client.get(reverse("portal:home"))
        self.assertContains(resp, "no longer an active employee", status_code=403)
        self.assertEqual(self.client.get(reverse("portal:payslips")).status_code, 403)

    def test_only_own_approved_payslips_are_shown(self):
        page = self.client.get(reverse("portal:payslips"))
        months = [(s.run.year, s.run.month) for s in page.context["slips"]]
        self.assertEqual(months, [(2026, 9), (2026, 8)])                                  # paid and approved, newest first
        self.assertNotContains(page, "123,456.78")                                       # a colleague's pay
        self.assertContains(page, "450,000.00")

    def test_payslips_that_are_not_theirs_or_not_final_are_unreachable(self):
        colleague = Payslip.objects.get(employee=self.tunde, run=self.runs["PAID"])
        for slip in (colleague, self.slips["DRAFT"], self.slips["PENDING"]):
            self.assertEqual(self.client.get(reverse("portal:payslip", args=[slip.pk])).status_code, 404, slip)
        self.assertEqual(self.client.get(reverse("portal:payslip", args=[99999])).status_code, 404)

    def test_a_payslip_shows_what_the_emailed_one_shows(self):
        page = self.client.get(reverse("portal:payslip", args=[self.slips["PAID"].pk]))
        self.assertContains(page, "Basic salary")
        self.assertContains(page, "PAYE tax")
        self.assertContains(page, "Alpha Ltd")
        self.assertContains(page, "450,000.00")
        self.assertNotContains(page, "455,000")                                          # the employer's total cost

    def test_home_shows_the_latest_payslip(self):
        page = self.client.get(reverse("portal:home"))
        self.assertContains(page, "2026-09")
        self.assertNotContains(page, "2026-10")                                          # the draft is hidden


class LeaveTests(PortalCase):
    def setUp(self):
        super().setUp()
        self.annual = LeaveType.objects.create(company=self.co, name="Annual", days_per_year=20, is_paid=True)
        self.client, self.account = self.login_as(self.ada)
        self.year = date.today().year

    def request_leave(self, start, end, **kw):
        return self.client.post(reverse("portal:leave_new"), {"leave_type": self.annual.pk, "start_date": start,
                                                              "end_date": end, **kw})

    def monday(self, weeks_ahead=2):
        today = date.today()
        return date.fromordinal(today.toordinal() - today.weekday() + 7 * weeks_ahead)

    def test_balance_and_request_flow(self):
        start = self.monday()
        end = date.fromordinal(start.toordinal() + 4)                                    # Monday to Friday
        resp = self.request_leave(start, end, reason="Family")
        self.assertRedirects(resp, reverse("portal:leave"), fetch_redirect_response=False)
        req = LeaveRequest.objects.get()
        self.assertEqual((req.employee, req.company, req.status, req.requested_by, req.reason),
                         (self.ada, self.co, "PENDING", self.account.user, "Family"))
        self.assertGreater(req.days, 0)
        page = self.client.get(reverse("portal:leave"))
        self.assertContains(page, "Annual")
        self.assertContains(page, "Withdraw")
        self.assertEqual(page.context["balances"][0][1]["pending"], req.days)

    def test_bad_requests_are_refused(self):
        start = self.monday()
        before_start = self.request_leave(start, date.fromordinal(start.toordinal() - 3))
        self.assertEqual(before_start.status_code, 200)
        too_long = self.request_leave(start, date.fromordinal(start.toordinal() + 120))
        self.assertEqual(too_long.status_code, 200)
        self.assertFalse(LeaveRequest.objects.exists())

    def test_requests_always_belong_to_the_signed_in_employee(self):
        start = self.monday()
        self.request_leave(start, date.fromordinal(start.toordinal() + 1), employee=self.tunde.pk,
                           relief_officer=self.tunde.pk)                                  # extra fields are ignored
        req = LeaveRequest.objects.get()
        self.assertEqual((req.employee, req.relief_officer), (self.ada, None))

    def test_withdrawing_and_the_limits_on_it(self):
        start = self.monday()
        self.request_leave(start, date.fromordinal(start.toordinal() + 1))
        req = LeaveRequest.objects.get()
        self.assertEqual(self.client.get(reverse("portal:leave_cancel", args=[req.pk])).status_code, 405)
        self.client.post(reverse("portal:leave_cancel", args=[req.pk]))
        req.refresh_from_db()
        self.assertEqual(req.status, "CANCELLED")
        approved = LeaveRequest.objects.create(company=self.co, employee=self.ada, leave_type=self.annual,
                                               start_date=start, end_date=start, days=1, status="APPROVED")
        resp = self.client.post(reverse("portal:leave_cancel", args=[approved.pk]), follow=True)
        self.assertContains(resp, "Ask HR to cancel")
        approved.refresh_from_db()
        self.assertEqual(approved.status, "APPROVED")

    def test_a_colleagues_leave_is_invisible_and_untouchable(self):
        theirs = LeaveRequest.objects.create(company=self.co, employee=self.tunde, leave_type=self.annual,
                                             start_date=self.monday(), end_date=self.monday(), days=1,
                                             status="PENDING", reason="Secret trip")
        page = self.client.get(reverse("portal:leave"))
        self.assertNotContains(page, "Secret trip")
        self.assertEqual(list(page.context["requests"]), [])
        self.assertEqual(self.client.post(reverse("portal:leave_cancel", args=[theirs.pk])).status_code, 404)
        theirs.refresh_from_db()
        self.assertEqual(theirs.status, "PENDING")

    def test_pages_vanish_when_the_leave_module_is_not_installed(self):
        real = services.has
        with mock.patch("portal.services.has", side_effect=lambda name: False if name == "leave" else real(name)):
            self.assertEqual(self.client.get(reverse("portal:leave")).status_code, 404)
            self.assertEqual(self.client.get(reverse("portal:leave_new")).status_code, 404)
            home = self.client.get(reverse("portal:home"))
            self.assertNotContains(home, "Leave this year")
            self.assertNotContains(home, reverse("portal:leave"))


class DetailsTests(PortalCase):
    def setUp(self):
        super().setUp()
        self.profile = EmployeeProfile.objects.create(
            employee=self.ada, job_title="Accountant", hire_date=date(2022, 3, 1), phone="0801", address="1 Road",
            tax_id="TIN-ADA", manager=self.tunde)
        self.client, self.account = self.login_as(self.ada)

    def test_shows_their_own_record_with_the_bank_account_hidden(self):
        page = self.client.get(reverse("portal:details"))
        for text in ("Accountant", "TIN-ADA", "Tunde Bello", "GTBank", "6789"):
            self.assertContains(page, text)
        self.assertNotContains(page, "0123456789")                                       # only the last four digits

    def test_only_phone_and_address_can_be_changed(self):
        self.client.post(reverse("portal:details_edit"), {
            "phone": "0999", "address": "9 New Street", "job_title": "Director", "tax_id": "HACKED",
            "manager": "", "hire_date": "2000-01-01"})
        self.profile.refresh_from_db()
        self.assertEqual((self.profile.phone, self.profile.address), ("0999", "9 New Street"))
        self.assertEqual((self.profile.job_title, self.profile.tax_id, self.profile.hire_date),
                         ("Accountant", "TIN-ADA", date(2022, 3, 1)))
        self.assertEqual(self.profile.manager, self.tunde)

    def test_without_an_hr_record_there_is_nothing_to_edit(self):
        self.profile.delete()
        resp = self.client.get(reverse("portal:details_edit"), follow=True)
        self.assertContains(resp, "HR has not set up your record yet")
        self.assertContains(self.client.get(reverse("portal:details")), "HR has not set up the rest")

    def test_emergency_contacts_add_edit_delete(self):
        self.client.post(reverse("portal:contact_new"), {"name": "Chi Obi", "relationship": "Wife", "phone": "0801",
                                                         "is_next_of_kin": "on"})
        contact = EmergencyContact.objects.get()
        self.assertEqual((contact.employee, contact.is_next_of_kin), (self.ada, True))
        self.client.post(reverse("portal:contact_edit", args=[contact.pk]),
                         {"name": "Chi Obi", "relationship": "Spouse", "phone": "0802"})
        contact.refresh_from_db()
        self.assertEqual((contact.relationship, contact.phone), ("Spouse", "0802"))
        self.assertEqual(self.client.get(reverse("portal:contact_delete", args=[contact.pk])).status_code, 405)
        self.client.post(reverse("portal:contact_delete", args=[contact.pk]))
        self.assertFalse(EmergencyContact.objects.exists())

    def test_a_colleagues_contacts_are_unreachable(self):
        theirs = EmergencyContact.objects.create(employee=self.tunde, name="Private", relationship="Mother", phone="1")
        self.assertNotContains(self.client.get(reverse("portal:details")), "Private")
        self.assertEqual(self.client.get(reverse("portal:contact_edit", args=[theirs.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("portal:contact_delete", args=[theirs.pk])).status_code, 404)
        self.assertTrue(EmergencyContact.objects.filter(pk=theirs.pk).exists())

    def test_pages_vanish_when_the_hr_module_is_not_installed(self):
        real = services.has
        with mock.patch("portal.services.has", side_effect=lambda name: False if name == "hr" else real(name)):
            self.assertEqual(self.client.get(reverse("portal:details")).status_code, 404)
            self.assertNotContains(self.client.get(reverse("portal:home")), reverse("portal:details"))


@skipUnless(apps.is_installed("audit"), "the audit log is not installed")
class AuditTests(PortalCase):
    def test_an_employees_own_changes_are_traced_to_their_login(self):
        from audit.models import AuditEntry
        EmployeeProfile.objects.create(employee=self.ada, job_title="X", hire_date=date(2022, 3, 1))
        client, _ = self.login_as(self.ada)
        client.post(reverse("portal:details_edit"), {"phone": "0999", "address": "New"})
        entry = AuditEntry.objects.get(model_label="hr.employeeprofile", action="UPDATE")
        self.assertEqual((entry.who, entry.employee_id), ("ada@a.test", self.ada.pk))
        self.assertEqual(entry.changes["phone"], ["", "0999"])
        self.assertTrue(AuditEntry.objects.filter(model_label="portal.employeeaccount", action="CREATE").exists())
        self.assertTrue(AuditEntry.objects.filter(action="LOGIN", username="ada@a.test").exists())
