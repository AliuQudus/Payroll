import pathlib
from datetime import date
from decimal import Decimal as D
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

import payroll
from payroll import integrations
from payroll import services as payroll_services
from django.test import Client

from payroll.models import Approver, Company, CompanyAccess, Employee, PayrollRun, StatutoryRule

from . import services
from .models import LeaveAllowanceApplication, LeaveEntitlement, LeaveRequest, LeaveType, PublicHoliday

MON, FRI = date(2026, 10, 5), date(2026, 10, 9)          # a Monday to the Friday of the same week


class LeaveTestCase(TestCase):
    def setUp(self):
        call_command("seed_demo", verbosity=0)
        call_command("seed_leave", verbosity=0)
        self.co = Company.objects.get()
        self.ada = Employee.objects.get(employee_no="E001")        # gross 500,000
        self.tunde = Employee.objects.get(employee_no="E002")
        users = get_user_model().objects
        self.maker = users.create_user("maker", password="x")
        self.checker = users.create_user("checker", password="x")
        self.admin = users.create_superuser("admin", password="x")
        self.annual = LeaveType.objects.get(name="Annual leave")
        self.unpaid = LeaveType.objects.get(name="Unpaid leave")

    def make(self, employee=None, start=MON, end=FRI, kind=None, status="PENDING"):
        kind = kind or self.annual
        days = services.count_days(start, end, kind.working_days_only, services.holiday_dates(self.co.pk))
        return LeaveRequest.objects.create(company=self.co, employee=employee or self.ada, leave_type=kind,
                                           start_date=start, end_date=end, days=days,
                                           requested_by=self.maker, status=status)

    def approve(self, req):
        services.decide(req, "approve", self.checker)
        req.refresh_from_db()

    def run_for(self, month):
        run = PayrollRun.objects.create(company=self.co, year=2026, month=month, created_by=self.maker)
        payroll_services.generate(run)
        return run

    def lines(self, run):
        return {l.code: l for l in run.payslips.get(employee=self.ada).lines.all()}

    def sign_in(self):
        self.client.login(username="admin", password="x")


class DayCountingTests(LeaveTestCase):
    def test_weekends_and_public_holidays_are_skipped(self):
        holidays = services.holiday_dates(self.co.pk)
        self.assertEqual(services.count_days(MON, date(2026, 10, 12), True, holidays), D(6))     # Mon to next Mon
        PublicHoliday.objects.create(company=self.co, date=date(2026, 10, 7), name="Test holiday")
        self.assertEqual(services.count_days(MON, date(2026, 10, 12), True, services.holiday_dates(self.co.pk)), D(5))
        self.assertEqual(services.count_days(MON, date(2026, 10, 12), False, holidays), D(8))   # calendar days


class RequestTests(LeaveTestCase):
    def test_a_request_is_saved_as_pending_with_its_days(self):
        self.sign_in()
        r = self.client.post(reverse("leave:new"), {
            "employee": self.ada.pk, "leave_type": self.annual.pk, "start_date": "2026-10-12",
            "end_date": "2026-10-16", "reason": "Family", "relief_officer": self.tunde.pk})
        self.assertRedirects(r, reverse("leave:requests"))
        req = LeaveRequest.objects.get()
        self.assertEqual((req.status, req.days, req.relief_officer), ("PENDING", D("5.0"), self.tunde))
        self.assertContains(self.client.get(reverse("leave:requests")), "Ada Okafor")

    def test_overlaps_and_balances_are_checked(self):
        self.sign_in()
        self.make()
        clash = self.client.post(reverse("leave:new"), {
            "employee": self.ada.pk, "leave_type": self.annual.pk, "start_date": "2026-10-08", "end_date": "2026-10-12"})
        self.assertContains(clash, "overlaps")
        LeaveEntitlement.objects.create(company=self.co, employee=self.tunde, leave_type=self.annual, year=2026, days=3)
        short = self.client.post(reverse("leave:new"), {
            "employee": self.tunde.pk, "leave_type": self.annual.pk, "start_date": "2026-11-02", "end_date": "2026-11-06"})
        self.assertContains(short, "Not enough Annual leave left")

    def test_relief_officer_must_be_available(self):
        self.sign_in()
        self.make(employee=self.tunde, status="APPROVED")
        r = self.client.post(reverse("leave:new"), {
            "employee": self.ada.pk, "leave_type": self.annual.pk, "start_date": "2026-10-05",
            "end_date": "2026-10-09", "relief_officer": self.tunde.pk})
        self.assertContains(r, "is on approved leave")


class ApprovalTests(LeaveTestCase):
    def test_a_different_user_must_approve(self):
        req = self.make()
        with self.assertRaises(ValueError):
            services.decide(req, "approve", self.maker)
        self.approve(req)
        self.assertEqual(req.status, "APPROVED")

    def test_decided_requests_can_be_opened_but_not_changed(self):
        self.sign_in()
        req = self.make()
        self.approve(req)
        page = self.client.get(reverse("leave:detail", args=[req.pk]))
        self.assertContains(page, "locked")
        with self.assertRaises(ValueError):
            services.decide(req, "reject", self.checker)                # a decided request cannot be re-decided
        rejected = self.make(employee=self.tunde)
        services.decide(rejected, "reject", self.checker, "Short staffed")
        page = self.client.get(reverse("leave:detail", args=[rejected.pk]))
        self.assertContains(page, "Short staffed")
        self.assertNotContains(page, "Approve</button>")

    def test_the_employee_is_emailed_about_the_decision(self):
        Company.objects.filter(pk=self.co.pk).update(email="hr@example.com", smtp_password="secret")
        Employee.objects.filter(pk=self.ada.pk).update(email="ada@example.com")
        self.sign_in()
        req = self.make()
        r = self.client.post(reverse("leave:action", args=[req.pk, "approve"]), follow=True)
        self.assertContains(r, "has been told by email")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["ada@example.com"])
        self.assertIn("approved", mail.outbox[0].subject)


class PayrollLinkTests(LeaveTestCase):
    def test_approved_unpaid_leave_reduces_pay(self):
        self.approve(self.make(kind=self.unpaid))
        run = self.run_for(10)
        slip = run.payslips.get(employee=self.ada)
        self.assertEqual(slip.gross, D("416666.67"))                     # 500,000 less 5 days at 500,000 / 30
        self.assertEqual(self.lines(run)["ABSENCE"].amount, D("83333.33"))

    def test_leave_allowance_is_worked_out_taxed_once_and_paid_once_a_year(self):
        req = self.make()
        self.approve(req)
        october = self.run_for(10)
        lines = self.lines(october)
        self.assertEqual(lines["LEAVEALLOW"].amount, D("240000.00"))      # Basic 200,000 x 12 x 10%
        slip = october.payslips.get(employee=self.ada)
        self.assertEqual((slip.paye, slip.net_pay), (D("108500.00"), D("591500.00")))
        self.assertEqual(LeaveAllowanceApplication.objects.filter(payslip__run=october).count(), 1)
        req.refresh_from_db()
        self.assertEqual(req.allowance, "Due in next payroll")           # still a draft run
        payroll_services.perform(october, "submit", self.maker)
        payroll_services.perform(october, "approve", self.checker)
        self.assertEqual(req.allowance, "Paid")
        with self.assertRaises(ValueError):
            services.decide(req, "cancel", self.checker)                  # the allowance is already paid
        self.assertNotIn("LEAVEALLOW", self.lines(self.run_for(11)))
        self.approve(self.make(start=date(2026, 12, 7), end=date(2026, 12, 11)))
        self.assertNotIn("LEAVEALLOW", self.lines(self.run_for(12)))      # one allowance per leave year

    def test_a_missing_allowance_rule_is_flagged_when_payroll_runs(self):
        self.approve(self.make())
        StatutoryRule.objects.filter(code="LEAVE_ALLOWANCE").delete()
        self.sign_in()
        run = PayrollRun.objects.create(company=self.co, year=2026, month=10, created_by=self.maker)
        r = self.client.post(reverse("run_action", args=[run.pk, "generate"]), follow=True)
        self.assertContains(r, "Leave allowance not paid for: Ada Okafor")

    def test_payroll_still_works_without_the_leave_module(self):
        self.approve(self.make(kind=self.unpaid))
        self.approve(self.make(start=date(2026, 10, 12), end=date(2026, 10, 16)))
        with mock.patch("payroll.integrations.apps.is_installed", return_value=False):
            run = self.run_for(10)
            self.assertEqual(integrations.unpaid_allowances(run), [])
        lines = self.lines(run)
        self.assertNotIn("LEAVEALLOW", lines)
        self.assertNotIn("ABSENCE", lines)

    def test_payroll_never_imports_leave_directly(self):
        root = pathlib.Path(payroll.__file__).parent
        for path in root.glob("*.py"):
            if path.name.startswith("test_") or path.name == "integrations.py":
                continue
            text = path.read_text()
            self.assertNotIn("import leave", text, path.name)
            self.assertNotIn("from leave", text, path.name)


class PageTests(LeaveTestCase):
    def test_every_leave_page_opens(self):
        self.sign_in()
        req = self.make()
        urls = [reverse("leave:requests"), reverse("leave:new"), reverse("leave:balances"), reverse("leave:detail", args=[req.pk])]
        for section in ("types", "holidays", "entitlements"):
            urls += [reverse("leave:section_list", args=[section]), reverse("leave:section_new", args=[section])]
        urls.append(reverse("leave:section_edit", args=["types", self.annual.pk]))
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 200, url)
        self.assertContains(self.client.get(reverse("leave:balances")), "Annual leave")
        self.assertContains(self.client.get(reverse("leave:requests")), ">Leave<")      # the module's own menu entry

    def test_a_leave_type_in_use_cannot_be_deleted(self):
        self.sign_in()
        self.make()
        r = self.client.post(reverse("leave:section_delete", args=["types", self.annual.pk]), follow=True)
        self.assertContains(r, "Mark it inactive")
        self.assertTrue(LeaveType.objects.filter(pk=self.annual.pk).exists())


class LeaveApproverTests(LeaveTestCase):
    def setUp(self):
        super().setUp()
        self.alice = get_user_model().objects.create_user("alice", password="x")
        Approver.objects.create(company=self.co, user=self.alice, scope="LEAVE")

    def test_only_the_designated_approver_can_decide(self):
        req = self.make()
        with self.assertRaises(ValueError) as ctx:
            services.decide(req, "approve", self.checker)
        self.assertIn("Only alice can approve leave requests", str(ctx.exception))
        with self.assertRaises(ValueError):
            services.decide(req, "reject", self.admin, "No")                  # administrators are not exempt
        services.decide(req, "approve", self.alice)
        req.refresh_from_db()
        self.assertEqual(req.status, "APPROVED")

    def test_cancelling_approved_leave_is_restricted_but_pending_is_not(self):
        pending = self.make(employee=self.tunde, start=date(2026, 11, 2), end=date(2026, 11, 6))
        services.decide(pending, "cancel", self.checker)
        approved = self.make()
        services.decide(approved, "approve", self.alice)
        with self.assertRaises(ValueError):
            services.decide(approved, "cancel", self.checker)
        services.decide(approved, "cancel", self.alice)

    def test_the_request_page_hides_decision_buttons_from_other_users(self):
        bob = get_user_model().objects.create_user("bob", password="x")
        for user in (bob, self.alice):
            CompanyAccess.objects.create(user=user, company=self.co)
        req = self.make()
        url = reverse("leave:detail", args=[req.pk])
        theirs = Client()
        theirs.login(username="bob", password="x")
        page = theirs.get(url)
        self.assertContains(page, "waiting for approval by alice")
        self.assertNotContains(page, "Approve</button>")
        hers = Client()
        hers.login(username="alice", password="x")
        self.assertContains(hers.get(url), "Approve</button>")
