from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from . import services
from .models import Company, PayrollRun


class RunFlowTests(TestCase):
    def setUp(self):
        call_command("seed_demo", verbosity=0)
        self.company = Company.objects.get()
        users = get_user_model().objects
        self.maker = users.create_user("maker", password="x")
        self.checker = users.create_user("checker", password="x")
        self.run = PayrollRun.objects.create(company=self.company, year=2026, month=10, created_by=self.maker)

    def test_generate_matches_engine(self):
        services.generate(self.run)
        slip = self.run.payslips.get(employee__employee_no="E001")
        self.assertEqual(slip.net_pay, D("394700.00"))
        self.assertTrue(slip.lines.filter(code="PAYE").exists())
        self.assertFalse(slip.lines.filter(code__in=["NHF", "NSITF"]).exists())

    def test_approval_flow_and_lock(self):
        services.generate(self.run)
        services.perform(self.run, "submit", self.maker)
        with self.assertRaises(ValueError):
            services.perform(self.run, "approve", self.maker)   # maker cannot approve own run
        services.perform(self.run, "approve", self.checker)
        self.assertEqual(self.run.status, "APPROVED")
        with self.assertRaises(ValueError):
            services.generate(self.run)                           # approved runs are locked

    def test_generate_explains_when_no_active_employees(self):
        self.company.employee_set.update(is_active=False)
        with self.assertRaises(ValueError) as ctx:
            services.generate(self.run)
        self.assertIn("no active employees", str(ctx.exception))

    def test_rules_are_entered_as_percentages(self):
        from datetime import date
        rules = services.load_rules(self.company, date(2026, 10, 1))
        self.assertEqual(rules.pension_employee_rate, D("0.08"))
        self.assertEqual(rules.pension_employer_rate, D("0.10"))
        self.assertEqual(rules.rent_relief_rate, D("0.20"))
        self.assertEqual(rules.rent_relief_cap, D("500000"))
        self.assertEqual(rules.bands[1][2], D("0.15"))
        self.assertEqual((rules.nhf_rate, rules.nsitf_rate, rules.itf_rate), (D("0"), D("0"), D("0")))
