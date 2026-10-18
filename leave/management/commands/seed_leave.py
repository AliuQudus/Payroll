from datetime import date

from django.core.management.base import BaseCommand, CommandError

from leave.models import LeaveType, PublicHoliday
from payroll.models import Company

# Starting points only: adjust the days and the holidays to your company's policy.
TYPES = [("Annual leave", 20, True, True), ("Sick leave", 12, True, False), ("Maternity leave", 90, True, False),
         ("Paternity leave", 10, True, False), ("Compassionate leave", 5, True, False), ("Unpaid leave", 0, False, False)]
HOLIDAYS = [(date(2026, 1, 1), "New Year's Day"), (date(2026, 5, 1), "Workers' Day"),
            (date(2026, 6, 12), "Democracy Day"), (date(2026, 10, 1), "Independence Day"),
            (date(2026, 12, 25), "Christmas Day"), (date(2026, 12, 26), "Boxing Day")]


class Command(BaseCommand):
    help = "Add the usual leave types and some public holidays to a company (default: the first company)."

    def add_arguments(self, parser):
        parser.add_argument("--company", help="Company name")

    def handle(self, *args, **options):
        qs = Company.objects.filter(name=options["company"]) if options["company"] else Company.objects.order_by("pk")
        company = qs.first()
        if company is None:
            raise CommandError("Create a company first.")
        for name, days, paid, allowance in TYPES:
            LeaveType.objects.get_or_create(company=company, name=name, defaults=dict(
                days_per_year=days, is_paid=paid, earns_allowance=allowance))
        for day, name in HOLIDAYS:
            PublicHoliday.objects.get_or_create(company=company, date=day, defaults=dict(name=name))
        self.stdout.write(f"Leave types and sample holidays added to {company.name}. Check them against your policy.")
