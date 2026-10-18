from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client, TestCase
from django.urls import reverse

from . import access, services
from .models import Approver, Company, CompanyAccess, PayrollRun

User = get_user_model()
GOOD = "Sturdy-pass-2026"


class UserTestCase(TestCase):
    def setUp(self):
        call_command("seed_demo", verbosity=0)
        self.co = Company.objects.get()
        self.other = Company.objects.create(name="Other Co")
        self.admin = User.objects.create_superuser("admin", password=GOOD)
        self.maker = self.staff("maker")
        self.alice = self.staff("alice")
        self.bob = self.staff("bob")
        self.client.login(username="admin", password=GOOD)

    def staff(self, username):
        user = User.objects.create_user(username, password=GOOD)
        CompanyAccess.objects.create(user=user, company=self.co)
        return user

    def as_user(self, username):
        client = Client()
        client.login(username=username, password=GOOD)
        return client

    def new_run(self, month, submit=True):
        run = PayrollRun.objects.create(company=self.co, year=2026, month=month, created_by=self.maker)
        services.generate(run)
        if submit:
            services.perform(run, "submit", self.maker)
        return run


class UserManagementTests(UserTestCase):
    def form(self, **extra):
        data = {"username": "dayo", "first_name": "Dayo", "last_name": "Ade", "email": "dayo@example.com",
                "role": "standard", "companies": [self.co.pk], "is_active": "on",
                "password1": GOOD, "password2": GOOD}
        data.update(extra)
        return data

    def test_an_administrator_creates_a_user_who_sees_only_their_company(self):
        r = self.client.post(reverse("user_new"), self.form())
        self.assertRedirects(r, reverse("user_list"))
        user = User.objects.get(username="dayo")
        self.assertTrue(user.check_password(GOOD))
        self.assertFalse(user.is_superuser)
        self.assertEqual(list(access.companies_for(user)), [self.co])
        self.assertEqual(set(access.companies_for(self.admin)), {self.co, self.other})
        theirs = self.as_user("dayo")
        self.assertEqual(theirs.get(reverse("section_list", args=["employees"])).status_code, 200)
        theirs.post(reverse("company_switch"), {"company": self.other.pk})
        self.assertNotEqual(theirs.session.get("company_id"), self.other.pk)

    def test_password_rules_are_enforced(self):
        self.assertContains(self.client.post(reverse("user_new"), self.form(password2="different")), "do not match")
        weak = self.client.post(reverse("user_new"), self.form(password1="12345678", password2="12345678"))
        self.assertContains(weak, "too common")
        self.assertFalse(User.objects.filter(username="dayo").exists())

    def test_a_standard_user_needs_a_company(self):
        r = self.client.post(reverse("user_new"), self.form(companies=[]))
        self.assertContains(r, "Choose at least one company")

    def test_editing_keeps_the_password_when_left_blank_and_can_make_an_administrator(self):
        r = self.client.post(reverse("user_edit", args=[self.bob.pk]), {
            "first_name": "Bob", "last_name": "B", "email": "", "role": "admin", "is_active": "on",
            "companies": [self.co.pk], "password1": "", "password2": ""})
        self.assertRedirects(r, reverse("user_list"))
        self.bob.refresh_from_db()
        self.assertTrue(self.bob.check_password(GOOD))
        self.assertTrue(self.bob.is_superuser and self.bob.is_staff)

    def test_you_cannot_lock_yourself_out(self):
        base = {"first_name": "", "last_name": "", "email": "", "companies": [self.co.pk], "password1": "", "password2": ""}
        demote = self.client.post(reverse("user_edit", args=[self.admin.pk]), dict(base, role="standard", is_active="on"))
        self.assertContains(demote, "cannot remove your own administrator access")
        off = self.client.post(reverse("user_edit", args=[self.admin.pk]), dict(base, role="admin"))
        self.assertContains(off, "cannot remove your own administrator access")
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_superuser and self.admin.is_active)


class AccessTests(UserTestCase):
    def test_only_administrators_reach_users_approvers_and_company_settings(self):
        theirs = self.as_user("maker")
        for name in ("user_list", "user_new", "approver_list", "company_edit", "company_new"):
            self.assertRedirects(theirs.get(reverse(name)), reverse("home"), fetch_redirect_response=False)
        self.assertEqual(theirs.post(reverse("company_test_email")).status_code, 302)
        for name in ("user_list", "approver_list", "company_edit"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200)
        self.assertNotContains(theirs.get(reverse("section_list", args=["elements"])), "Approvers")
        self.assertContains(self.client.get(reverse("section_list", args=["elements"])), "Approvers")

    def test_a_user_without_any_company_is_told_so(self):
        User.objects.create_user("nobody", password=GOOD)
        r = self.as_user("nobody").get(reverse("section_list", args=["employees"]))
        self.assertContains(r, "not been given access", status_code=403)


class ApproverPageTests(UserTestCase):
    def test_designating_and_removing_approvers(self):
        carol = User.objects.create_user("carol", password=GOOD)               # no access to the company
        page = self.client.get(reverse("approver_list"))
        choices = set(page.context["form"].fields["user"].queryset)
        self.assertIn(self.alice, choices)
        self.assertIn(self.admin, choices)
        self.assertNotIn(carol, choices)
        r = self.client.post(reverse("approver_list"), {"user": self.alice.pk, "scope": "PAYROLL"})
        self.assertRedirects(r, reverse("approver_list"))
        self.assertEqual(access.approver_names(self.co, "PAYROLL"), ["alice"])
        again = self.client.post(reverse("approver_list"), {"user": self.alice.pk, "scope": "PAYROLL"}, follow=True)
        self.assertContains(again, "already an approver")
        item = Approver.objects.get()
        self.client.post(reverse("approver_delete", args=[item.pk]))
        self.assertFalse(Approver.objects.exists())


class PayrollApprovalTests(UserTestCase):
    def test_without_designation_any_other_user_can_approve(self):
        run = self.new_run(10)
        services.perform(run, "approve", self.bob)
        self.assertEqual(run.status, "APPROVED")

    def test_once_designated_only_they_can_approve(self):
        Approver.objects.create(company=self.co, user=self.alice, scope="PAYROLL")
        run = self.new_run(11)
        with self.assertRaises(ValueError) as ctx:
            services.perform(run, "approve", self.bob)
        self.assertIn("Only alice can approve payroll runs", str(ctx.exception))
        with self.assertRaises(ValueError):
            services.perform(run, "approve", self.admin)                      # administrators are not exempt
        services.perform(run, "approve", self.alice)
        self.assertEqual((run.status, run.approved_by), ("APPROVED", self.alice))

    def test_a_deactivated_sole_approver_does_not_open_approval_to_everyone(self):
        Approver.objects.create(company=self.co, user=self.alice, scope="PAYROLL")
        User.objects.filter(pk=self.alice.pk).update(is_active=False)
        run = self.new_run(12)
        with self.assertRaises(ValueError) as ctx:
            services.perform(run, "approve", self.bob)
        self.assertIn("deactivated", str(ctx.exception))

    def test_the_run_page_follows_the_designation(self):
        Approver.objects.create(company=self.co, user=self.alice, scope="PAYROLL")
        run = self.new_run(10)
        url = reverse("run_detail", args=[run.pk])
        bob = self.as_user("bob")
        page = bob.get(url)
        self.assertContains(page, "waiting for approval by alice")
        self.assertNotContains(page, "Approve</button>")
        refused = bob.post(reverse("run_action", args=[run.pk, "approve"]), follow=True)
        self.assertContains(refused, "Only alice can approve")
        alice = self.as_user("alice")
        self.assertContains(alice.get(url), "Approve</button>")
        done = alice.post(reverse("run_action", args=[run.pk, "approve"]), follow=True)
        run.refresh_from_db()
        self.assertEqual(run.status, "APPROVED")
        self.assertContains(done, "Approved")
