from datetime import date
from decimal import Decimal as D

from django.core.management.base import BaseCommand

from payroll.models import Company, Employee, SalaryElement, StatutoryRule, TaxBand

START = date(2026, 1, 1)
BANDS = [(0, 800000, "0"), (800000, 3000000, "15"), (3000000, 12000000, "18"),
         (12000000, 25000000, "21"), (25000000, 50000000, "23"), (50000000, None, "25")]
RULES = {"PENSION_EMPLOYEE": "8", "PENSION_EMPLOYER": "10", "ABSENCE_DIVISOR": "30",
         "RENT_RELIEF_RATE": "20", "RENT_RELIEF_CAP": "500000", "LEAVE_ALLOWANCE": "10"}   # percentages, except the cap and days


class Command(BaseCommand):
    help = "Create a demo company with sample elements, PAYE bands, rules and employees."

    def handle(self, *args, **options):
        co, _ = Company.objects.get_or_create(name="Demo Company")
        for code, name, formula, nhf in [("BASIC", "Basic", "GROSS*0.4", True),
                                          ("HOUSING", "Housing", "GROSS*0.3", False),
                                          ("TRANSPORT", "Transport", "GROSS*0.3", False)]:
            SalaryElement.objects.get_or_create(company=co, code=code, defaults=dict(
                name=name, kind="EARNING", formula=formula, is_taxable=True,
                is_pensionable=True, in_nhf_base=nhf, in_leave_base=(code == "BASIC")))
        for lo, hi, rate in BANDS:
            TaxBand.objects.get_or_create(company=co, effective_from=START, lower_limit=lo,
                                          defaults=dict(upper_limit=hi, rate=D(rate)))
        for code, value in RULES.items():
            StatutoryRule.objects.get_or_create(company=co, code=code, effective_from=START,
                                                defaults=dict(value=D(value)))
        for no, first, last, gross, rent in [("E001", "Ada", "Okafor", 500000, 0),
                                              ("E002", "Tunde", "Bello", 850000, 1200000),
                                              ("E003", "Ngozi", "Eze", 250000, 0)]:
            Employee.objects.get_or_create(company=co, employee_no=no, defaults=dict(
                first_name=first, last_name=last, gross_monthly=gross, annual_rent=rent))
        self.stdout.write("Demo data ready. PAYE bands are sample figures: verify them before real use.")
