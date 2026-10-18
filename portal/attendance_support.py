"""Attendance and overtime pages for the portal. Imported only when the attendance module is installed."""
from collections import Counter
from datetime import date
from decimal import Decimal

from django import forms
from django.db.models import Sum

from attendance import services
from attendance.models import AttendanceRecord, OvertimeRequest


class OvertimeForm(forms.Form):
    date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
                           help_text="The day you worked the overtime.")
    hours = forms.DecimalField(max_digits=4, decimal_places=2, min_value=Decimal("0.25"),
                               help_text="Hours of overtime that day, for example 2.5.")
    reason = forms.CharField(max_length=300, required=False, help_text="What the extra work was for.")


def month_of(employee, first):
    """(daily records, counts by status, approved overtime hours and amount) for one employee and month."""
    records = list(AttendanceRecord.objects.filter(employee=employee, date__year=first.year,
                                                   date__month=first.month).order_by("date"))
    counts = Counter(r.status for r in records)
    approved = OvertimeRequest.objects.filter(employee=employee, status=OvertimeRequest.Status.APPROVED,
                                              date__year=first.year, date__month=first.month).aggregate(
        hours=Sum("hours"), amount=Sum("amount"))
    return records, counts, approved["hours"] or Decimal("0"), approved["amount"] or Decimal("0")


def my_requests(employee):
    return OvertimeRequest.objects.filter(employee=employee)


def paid_ids(requests):
    return {r.pk for r in requests if r.status == OvertimeRequest.Status.APPROVED and services.is_paid(r)}


def request_overtime(employee, user, day, hours, reason):
    return services.add_request(employee, day, hours, reason, user)


def withdraw(req, user):
    services.decide(req, "cancel", user)


def home_figures(employee):
    first = date.today().replace(day=1)
    _, counts, _, _ = month_of(employee, first)
    absent = counts.get(AttendanceRecord.Status.ABSENT, 0) + Decimal("0.5") * counts.get(
        AttendanceRecord.Status.HALF_DAY, 0)
    waiting = my_requests(employee).filter(status=OvertimeRequest.Status.PENDING).count()
    return absent, waiting
