from datetime import date
from decimal import Decimal as D
from unittest import mock

from django.apps import apps
from django.core.management import call_command
from django.test import TestCase

from payroll import services as payroll_services
from payroll.models import Company, Employee, EmployeeAdjustment, PayrollRun

from .models import EmployeeProfile


def lines(slip):
    return {l.code: l for l in slip.lines.all()}


class StarterCase(TestCase):
    """Pay for someone who joins part-way through a month, using the real payroll engine."""

    def setUp(self):
        call_command("seed_demo", verbosity=0)
        self.co = Company.objects.get()
        self.ada = Employee.objects.get(employee_no="E001")
        self.ada.gross_monthly = D("500000")
        self.ada.save()
        self.tunde = Employee.objects.get(employee_no="E002")

    def hired(self, day, employee=None):
        return EmployeeProfile.objects.update_or_create(
            employee=employee or self.ada, defaults={"job_title": "Accountant", "hire_date": day})[0]

    def run_month(self, year, month):
        run = PayrollRun.objects.create(company=self.co, year=year, month=month)
        payroll_services.generate(run)
        return run

    def slip(self, run, employee=None):
        return run.payslips.get(employee=employee or self.ada)


class StartDateTests(StarterCase):
    def test_the_first_month_is_paid_from_the_start_date(self):
        self.hired(date(2026, 9, 20))                                          # 20th to 30th is 11 of 30 days
        slip = self.slip(self.run_month(2026, 9))
        self.assertEqual(slip.gross, D("183333.33"))                           # 500,000 x 11 / 30
        part = lines(slip)["PRORATA"]
        self.assertEqual(part.amount, D("316666.67"))
        self.assertIn("11 of 30", part.name)
        self.assertEqual(self.slip(PayrollRun.objects.get(month=9), self.tunde).gross, self.tunde.gross_monthly)

    def test_starting_on_the_first_pays_the_whole_month(self):
        self.hired(date(2026, 9, 1))
        slip = self.slip(self.run_month(2026, 9))
        self.assertEqual(slip.gross, D("500000.00"))
        self.assertNotIn("PRORATA", lines(slip))

    def test_nothing_is_paid_before_the_start_month(self):
        self.hired(date(2026, 10, 15))
        self.assertFalse(self.run_month(2026, 9).payslips.filter(employee=self.ada).exists())
        self.assertTrue(PayrollRun.objects.get(month=9).payslips.filter(employee=self.tunde).exists())
        october = self.slip(self.run_month(2026, 10))
        self.assertEqual(october.gross, D("274193.55"))                        # 500,000 x 17 / 31
        self.assertIn("17 of 31", lines(october)["PRORATA"].name)              # 15th to 31st

    def test_every_month_after_the_first_is_full(self):
        self.hired(date(2026, 9, 20))
        self.run_month(2026, 9)
        self.assertEqual(self.slip(self.run_month(2026, 10)).gross, D("500000.00"))

    def test_a_start_date_in_a_later_year_is_not_paid_yet(self):
        self.hired(date(2027, 1, 5))
        self.assertFalse(self.run_month(2026, 9).payslips.filter(employee=self.ada).exists())

    def test_someone_with_no_hr_record_is_paid_as_before(self):
        self.assertEqual(self.slip(self.run_month(2026, 9)).gross, D("500000.00"))

    def test_joining_and_leaving_in_the_same_month(self):
        from exits import services as exit_services
        self.hired(date(2026, 9, 10))
        exit_services.start_exit(self.ada, "RESIGNATION", date(2026, 9, 20))   # 10th to 20th is 11 of 30 days
        slip = self.slip(self.run_month(2026, 9))
        self.assertEqual(slip.gross, D("183333.33"))
        self.assertIn("11 of 30", lines(slip)["PRORATA"].name)

    def test_absence_is_still_worked_out_on_the_full_salary(self):
        self.hired(date(2026, 9, 20))
        EmployeeAdjustment.objects.create(company=self.co, employee=self.ada, kind="ABSENCE", description="Away",
                                          start_year=2026, start_month=9, days=1)
        slip = self.slip(self.run_month(2026, 9))
        self.assertEqual(lines(slip)["ABSENCE"].amount, D("16666.67"))        # a day at 500,000 / 30
        self.assertEqual(slip.gross, D("183333.33") - D("16666.67"))

    def test_without_the_hr_module_nobody_is_prorated(self):
        self.hired(date(2026, 9, 20))
        real = apps.is_installed
        with mock.patch("payroll.integrations.apps.is_installed", side_effect=lambda name: name != "hr" and real(name)):
            self.assertEqual(self.slip(self.run_month(2026, 9)).gross, D("500000.00"))
