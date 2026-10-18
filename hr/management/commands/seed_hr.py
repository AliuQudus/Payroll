from datetime import date

from django.core.management.base import BaseCommand

from hr.models import EmergencyContact, EmployeeProfile
from payroll.models import Employee

TITLES = ["Finance Manager", "Accountant", "Administrative Officer", "Operations Officer", "HR Officer"]


class Command(BaseCommand):
    help = "Give the demo employees an HR record (safe to run again)."

    def handle(self, *args, **options):
        people = list(Employee.objects.filter(is_active=True).order_by("company_id", "employee_no"))
        boss = {}
        made = 0
        for i, emp in enumerate(people):
            boss.setdefault(emp.company_id, emp)
            _, created = EmployeeProfile.objects.get_or_create(employee=emp, defaults={
                "job_title": TITLES[i % len(TITLES)],
                "hire_date": date(2021 + i % 4, 1 + (i * 3) % 12, 1 + (i * 7) % 28),
                "manager": None if boss[emp.company_id] == emp else boss[emp.company_id],
            })
            made += created
            if created:
                EmergencyContact.objects.create(employee=emp, name="Sample Contact", relationship="Spouse",
                                                phone="08000000000", is_next_of_kin=True)
        self.stdout.write(f"Created {made} HR record(s).")
