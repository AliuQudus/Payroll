from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from . import services
from .models import Company, Employee, EmployeeAdjustment, PayrollRun


class AdjustmentTests(TestCase):
    def setUp(self):
        call_command("seed_demo", verbosity=0)
        self.co = Company.objects.get()
        self.emp = Employee.objects.get(employee_no="E001")      # gross 500,000
        users = get_user_model().objects
        self.maker = users.create_user("maker", password="x")
        self.checker = users.create_user("checker", password="x")

    def add(self, kind, **kw):
        kw.setdefault("start_year", 2026)
        kw.setdefault("start_month", 10)
        return EmployeeAdjustment.objects.create(company=self.co, employee=self.emp, kind=kind, **kw)

    def run_for(self, month):
        run = PayrollRun.objects.create(company=self.co, year=2026, month=month, created_by=self.maker)
        services.generate(run)
        return run

    def approve(self, run):
        services.perform(run, "submit", self.maker)
        services.perform(run, "approve", self.checker)

    def lines(self, run):
        return {l.code: l for l in run.payslips.get(employee=self.emp).lines.all()}

    def test_all_types_are_picked_up(self):
        add = self.add("ADDITION", description="Bonus", amount=20000)
        ded = self.add("DEDUCTION", description="Union dues", amount=5000)
        iou = self.add("IOU", description="Cash advance", amount=12000, months=3)
        loan = self.add("LOAN", description="Staff loan", amount=30000, months=3)
        self.add("ABSENCE", description="Unpaid leave", days=3)
        run = self.run_for(10)
        lines = self.lines(run)
        self.assertEqual(lines[f"ADD{add.pk}"].amount, D("20000.00"))
        self.assertEqual(lines[f"DED{ded.pk}"].amount, D("5000.00"))
        self.assertEqual(lines[f"IOU{iou.pk}"].amount, D("4000.00"))
        self.assertEqual(lines[f"LOAN{loan.pk}"].amount, D("10000.00"))
        self.assertEqual(lines["ABSENCE"].amount, D("50000.00"))          # 500,000 / 30 x 3 days
        self.assertEqual(run.payslips.get(employee=self.emp).gross, D("450000.00"))

    def test_nothing_applies_outside_its_period(self):
        self.add("DEDUCTION", description="Union dues", amount=5000, start_month=11)
        self.assertFalse([c for c in self.lines(self.run_for(10)) if c.startswith("DED")])

    def test_loan_splits_into_installments_and_becomes_treated(self):
        loan = self.add("LOAN", description="Staff loan", amount=20000, months=2)
        october = self.run_for(10)
        self.assertEqual(self.lines(october)[f"LOAN{loan.pk}"].amount, D("10000.00"))
        loan.refresh_from_db()
        self.assertEqual(loan.applied, D("0"))                           # draft runs do not count yet
        self.approve(october)
        loan.refresh_from_db()
        self.assertEqual((loan.applied, loan.remaining, loan.progress), (D("10000.00"), D("10000.00"), "1 of 2"))
        self.assertFalse(loan.is_treated)
        november = self.run_for(11)
        self.assertEqual(self.lines(november)[f"LOAN{loan.pk}"].amount, D("10000.00"))
        self.approve(november)
        loan.refresh_from_db()
        self.assertTrue(loan.is_treated)
        self.assertEqual(loan.status, "Treated")
        self.assertFalse([c for c in self.lines(self.run_for(12)) if c.startswith("LOAN")])

    def test_other_types_become_treated_when_done(self):
        once = self.add("DEDUCTION", description="Fine", amount=5000)
        twice = self.add("ADDITION", description="Bonus", amount=1000, months=2)
        absence = self.add("ABSENCE", description="Leave", days=2)
        self.approve(self.run_for(10))
        for a in (once, twice, absence):
            a.refresh_from_db()
        self.assertTrue(once.is_treated)
        self.assertFalse(twice.is_treated)
        self.assertTrue(absence.is_treated)
        self.assertEqual(twice.remaining, D("1000.00"))
        self.assertEqual(absence.applied, D("33333.33"))                  # 500,000 / 30 x 2 days

    def test_rent_relief_shows_on_payslip(self):
        bello = Employee.objects.get(employee_no="E002")                  # rent 1,200,000 a year
        line = self.run_for(10).payslips.get(employee=bello).lines.get(code="RENT_RELIEF")
        self.assertEqual((line.kind, line.amount), ("INFO", D("20000.00")))   # 240,000 a year / 12

    def test_pages_create_a_loan(self):
        get_user_model().objects.create_superuser("admin", password="x")
        self.client.login(username="admin", password="x")
        r = self.client.post(reverse("section_new", args=["loans"]), {
            "employee": self.emp.pk, "description": "Laptop loan", "amount": "60000", "months": 3,
            "start_year": 2026, "start_month": 10, "is_active": "on"})
        self.assertRedirects(r, reverse("section_list", args=["loans"]))
        page = self.client.get(reverse("section_list", args=["loans"]))
        self.assertContains(page, "Laptop loan")
        self.assertContains(page, "20000.00")                             # per installment
        bad = self.client.post(reverse("section_new", args=["loans"]), {
            "employee": self.emp.pk, "description": "x", "amount": "100", "months": 0,
            "start_year": 2026, "start_month": 10})
        self.assertContains(bad, "Must be at least 1")

    def test_treated_records_are_locked_and_filtered(self):
        done = self.add("DEDUCTION", description="Old fine", amount=100, is_treated=True)
        self.add("DEDUCTION", description="New fine", amount=100)
        get_user_model().objects.create_superuser("admin", password="x")
        self.client.login(username="admin", password="x")
        url = reverse("section_list", args=["deductions"])
        page = self.client.get(url)
        self.assertContains(page, "New fine")
        self.assertNotContains(page, "Old fine")
        self.assertContains(self.client.get(url + "?status=treated"), "Old fine")
        self.assertContains(self.client.get(url + "?status=all"), "Old fine")
        view_url = reverse("section_view", args=["deductions", done.pk])
        self.assertRedirects(self.client.get(reverse("section_edit", args=["deductions", done.pk])), view_url)
        page = self.client.get(view_url)                       # a treated record can be opened...
        self.assertContains(page, "Old fine")
        self.assertContains(page, "locked")
        self.assertNotContains(page, ">Edit<")                 # ...but not edited
        self.assertEqual(self.client.post(reverse("section_edit", args=["deductions", done.pk]),
                                          {"description": "Changed"}).status_code, 302)
        done.refresh_from_db()
        self.assertEqual(done.description, "Old fine")
        self.client.post(reverse("section_delete", args=["deductions", done.pk]))
        self.assertTrue(EmployeeAdjustment.objects.filter(pk=done.pk).exists())
