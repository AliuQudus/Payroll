from datetime import date, timedelta
from decimal import Decimal as D
from unittest import mock, skipUnless

from django.apps import apps
from django.test import Client
from django.urls import reverse

from . import services
from .test_portal import PortalCase

# These pages only exist when the attendance module is installed, so everything here is skipped without it.
HAS_ATTENDANCE = apps.is_installed("attendance")
if HAS_ATTENDANCE:
    from attendance import services as attendance_services
    from attendance.models import AttendanceRecord, AttendanceSetting, OvertimeApplication, OvertimeRequest

S = AttendanceRecord.Status if HAS_ATTENDANCE else None
WEDNESDAY, SATURDAY = date(2026, 9, 9), date(2026, 9, 12)


@skipUnless(HAS_ATTENDANCE, "the attendance module is not installed")
class AttendancePortalCase(PortalCase):
    def setUp(self):
        super().setUp()
        # round figures: 500,000 / 160 hours = 3,125 an hour
        AttendanceSetting.objects.create(company=self.co, hours_per_month=D("160"), base_percent=D("100"),
                                         weekday_rate=D("1.5"), weekend_rate=D("2"), holiday_rate=D("2"))
        self.client, self.account = self.login_as(self.ada)

    def mark(self, day, status, employee=None, note=""):
        return AttendanceRecord.objects.create(company=self.co, employee=employee or self.ada, date=day,
                                               status=status, note=note)

    def request_overtime(self, day=WEDNESDAY, hours="4", **extra):
        return self.client.post(reverse("portal:overtime_new"), {"date": day.isoformat(), "hours": hours,
                                                                  "reason": "Month end", **extra})

    def approved_overtime(self, day=WEDNESDAY, hours="4", employee=None):
        req = attendance_services.add_request(employee or self.ada, day, D(hours), "", self.admin)
        attendance_services.decide(req, "approve", self.admin)
        req.refresh_from_db()
        return req


class AttendancePageTests(AttendancePortalCase):
    def test_every_page_needs_a_login(self):
        for name in ("attendance", "overtime", "overtime_new"):
            resp = Client().get(reverse(f"portal:{name}"))
            self.assertEqual(resp.status_code, 302, name)
            self.assertIn("login", resp["Location"])

    def test_the_month_shows_only_my_records(self):
        for day, status in ((date(2026, 9, 7), S.PRESENT), (date(2026, 9, 8), S.PRESENT), (WEDNESDAY, S.LATE),
                            (date(2026, 9, 10), S.ABSENT), (date(2026, 9, 11), S.HALF_DAY)):
            self.mark(day, status)
        self.mark(date(2026, 8, 31), S.ABSENT)                                          # another month
        self.mark(date(2026, 9, 7), S.ABSENT, employee=self.tunde, note="Colleague secret")
        self.approved_overtime()                                                        # 4 h on a weekday: 18,750
        self.approved_overtime(SATURDAY, "2", employee=self.tunde)                      # a colleague's
        page = self.client.get(reverse("portal:attendance"), {"month": "2026-09"})
        self.assertEqual(len(page.context["records"]), 5)
        self.assertEqual(dict(page.context["counts"]), {"PRESENT": 2, "LATE": 1, "ABSENT": 1, "HALF_DAY": 1})
        self.assertEqual((page.context["ot_hours"], page.context["ot_amount"]), (D("4.00"), D("18750.00")))
        self.assertContains(page, "September 2026")
        self.assertContains(page, "18,750.00")
        self.assertNotContains(page, "Colleague secret")
        self.assertNotContains(page, "25,000.00")                                       # their overtime pay

    def test_navigation_and_odd_months(self):
        page = self.client.get(reverse("portal:attendance"), {"month": "2026-09"})
        self.assertEqual(page.context["prev"], date(2026, 8, 1))
        self.assertEqual(self.client.get(reverse("portal:attendance"), {"month": "garbage"}).status_code, 200)
        this_month = date.today().replace(day=1).strftime("%Y-%m")
        self.assertIsNone(self.client.get(reverse("portal:attendance"), {"month": this_month}).context["next"])
        self.assertEqual(self.client.get(reverse("portal:attendance"), {"month": "2999-01"}).status_code, 200)

    def test_home_shows_this_months_absence_and_waiting_overtime(self):
        today = date.today()
        first = today.replace(day=1)
        self.mark(first, S.ABSENT)
        expected = D("1")
        if first + timedelta(days=1) <= today:                                         # a half day, when the month allows
            self.mark(first + timedelta(days=1), S.HALF_DAY)
            expected = D("1.5")
        attendance_services.add_request(self.ada, first, D("1"), "", self.admin)
        home = self.client.get(reverse("portal:home"))
        self.assertEqual((home.context["absent_days"], home.context["overtime_waiting"]), (expected, 1))
        self.assertContains(home, "This month")
        self.assertContains(home, reverse("portal:overtime"))
        self.assertContains(home, "reduce this month")

    def test_the_menu_has_the_new_links(self):
        page = self.client.get(reverse("portal:home"))
        self.assertContains(page, reverse("portal:attendance"))
        self.assertContains(page, reverse("portal:overtime"))


class OvertimeRequestTests(AttendancePortalCase):
    def test_requesting_overtime(self):
        resp = self.request_overtime(SATURDAY, "2.5")
        self.assertRedirects(resp, reverse("portal:overtime"), fetch_redirect_response=False)
        req = OvertimeRequest.objects.get()
        self.assertEqual((req.employee, req.company, req.status, req.requested_by, req.day_type, req.hours, req.reason),
                         (self.ada, self.co, "PENDING", self.account.user, "WEEKEND", D("2.50"), "Month end"))
        self.assertIsNone(req.amount)                                                   # priced only when approved
        page = self.client.get(reverse("portal:overtime"))
        self.assertContains(page, "Withdraw")
        self.assertContains(page, "Waiting for approval")

    def test_a_request_is_always_for_the_signed_in_employee(self):
        self.request_overtime(employee=self.tunde.pk, status="APPROVED", amount="999999", hourly_rate="9999")
        req = OvertimeRequest.objects.get()
        self.assertEqual((req.employee, req.status, req.amount), (self.ada, "PENDING", None))

    def test_invalid_requests_are_explained_and_not_saved(self):
        tomorrow = date.today() + timedelta(days=1)
        self.assertContains(self.request_overtime(tomorrow), "already happened")
        for hours in ("0", "0.1", "-2", "abc"):
            self.assertEqual(self.request_overtime(hours=hours).status_code, 200, hours)
        self.assertContains(self.request_overtime(hours="13"), "at most 12")
        self.assertFalse(OvertimeRequest.objects.exists())
        self.request_overtime(hours="8")
        self.assertContains(self.request_overtime(hours="5"), "already has")             # the day would exceed 12 hours
        self.assertEqual(OvertimeRequest.objects.count(), 1)

    def test_the_list_shows_only_my_requests(self):
        attendance_services.add_request(self.tunde, WEDNESDAY, D("3"), "Colleague reason", self.admin)
        self.request_overtime()
        page = self.client.get(reverse("portal:overtime"))
        self.assertEqual([r.employee for r in page.context["requests"]], [self.ada])
        self.assertNotContains(page, "Colleague reason")

    def test_paid_overtime_is_marked_paid(self):
        req = self.approved_overtime()
        self.assertNotContains(self.client.get(reverse("portal:overtime")), ">Paid<")
        OvertimeApplication.objects.create(request=req, payslip=self.slips["PAID"], amount=req.amount)
        page = self.client.get(reverse("portal:overtime"))
        self.assertEqual(page.context["paid"], {req.pk})
        self.assertContains(page, "18,750.00")

    def test_withdrawing_and_its_limits(self):
        self.request_overtime()
        req = OvertimeRequest.objects.get()
        self.assertEqual(self.client.get(reverse("portal:overtime_cancel", args=[req.pk])).status_code, 405)
        self.client.post(reverse("portal:overtime_cancel", args=[req.pk]))
        req.refresh_from_db()
        self.assertEqual((req.status, req.decided_by), ("CANCELLED", self.account.user))
        again = self.client.post(reverse("portal:overtime_cancel", args=[req.pk]), follow=True)
        self.assertContains(again, "Ask HR")                                            # already closed
        approved = self.approved_overtime(SATURDAY, "2")
        resp = self.client.post(reverse("portal:overtime_cancel", args=[approved.pk]), follow=True)
        self.assertContains(resp, "Ask HR")
        approved.refresh_from_db()
        self.assertEqual(approved.status, "APPROVED")

    def test_a_colleagues_request_cannot_be_touched(self):
        theirs = attendance_services.add_request(self.tunde, WEDNESDAY, D("3"), "", self.admin)
        self.assertEqual(self.client.post(reverse("portal:overtime_cancel", args=[theirs.pk])).status_code, 404)
        theirs.refresh_from_db()
        self.assertEqual(theirs.status, "PENDING")

    def test_staff_approval_shows_up_for_the_employee(self):
        self.request_overtime()
        req = OvertimeRequest.objects.get()
        staff = self.admin_client.post(reverse("attendance:overtime_decide", args=[req.pk, "approve"]),
                                       {"note": "Thanks"}, follow=True)
        self.assertContains(staff, "Overtime approved")
        page = self.client.get(reverse("portal:overtime"))
        self.assertContains(page, "Approved")
        self.assertContains(page, "18,750.00")                                          # 3,125 x 1.5 x 4
        self.assertContains(page, "Thanks")
        self.assertNotContains(page, "Withdraw")

    def test_an_employee_cannot_approve_their_own_request(self):
        self.request_overtime()
        req = OvertimeRequest.objects.get()
        resp = self.client.post(reverse("attendance:overtime_decide", args=[req.pk, "approve"]))
        self.assertIn(resp.status_code, (302, 403))
        req.refresh_from_db()
        self.assertEqual(req.status, "PENDING")


class AttendanceAccessTests(AttendancePortalCase):
    def test_the_staff_attendance_screens_are_closed_to_an_employee_login(self):
        req = attendance_services.add_request(self.tunde, WEDNESDAY, D("3"), "", self.admin)
        pages = [reverse("attendance:register"), reverse("attendance:summary"), reverse("attendance:summary_csv"),
                 reverse("attendance:import"), reverse("attendance:overtime"), reverse("attendance:overtime_new"),
                 reverse("attendance:overtime_detail", args=[req.pk]), reverse("attendance:settings")]
        for url in pages:
            resp = self.client.get(url)
            self.assertIn(resp.status_code, (302, 403), url)
            self.assertNotContains(resp, "Tunde", status_code=resp.status_code)
        self.assertIn(self.client.post(reverse("attendance:overtime_decide", args=[req.pk, "approve"])).status_code,
                      (302, 403))
        self.assertEqual(OvertimeRequest.objects.get(pk=req.pk).status, "PENDING")

    def test_an_employee_who_has_left_loses_the_pages(self):
        self.ada.is_active = False
        self.ada.save()
        for name in ("attendance", "overtime", "overtime_new"):
            self.assertEqual(self.client.get(reverse(f"portal:{name}")).status_code, 403, name)

    def test_the_pages_vanish_when_the_attendance_module_is_not_installed(self):
        real = services.has
        with mock.patch("portal.services.has", side_effect=lambda name: False if name == "attendance" else real(name)):
            for name in ("attendance", "overtime", "overtime_new"):
                self.assertEqual(self.client.get(reverse(f"portal:{name}")).status_code, 404, name)
            self.assertEqual(self.client.post(reverse("portal:overtime_cancel", args=[1])).status_code, 404)
            home = self.client.get(reverse("portal:home"))
            self.assertNotContains(home, "This month")
            self.assertNotContains(home, reverse("portal:attendance"))


@skipUnless(apps.is_installed("audit"), "the audit log is not installed")
class AuditTests(AttendancePortalCase):
    def test_an_employees_overtime_request_is_traced_to_their_login(self):
        from audit.models import AuditEntry
        self.request_overtime()
        entry = AuditEntry.objects.get(model_label="attendance.overtimerequest", action="CREATE")
        self.assertEqual((entry.who, entry.employee_id), ("ada@a.test", self.ada.pk))
        req = OvertimeRequest.objects.get()
        self.client.post(reverse("portal:overtime_cancel", args=[req.pk]))
        change = AuditEntry.objects.get(model_label="attendance.overtimerequest", action="UPDATE")
        self.assertEqual((change.who, change.changes["status"]), ("ada@a.test", ["Waiting for approval", "Cancelled"]))
