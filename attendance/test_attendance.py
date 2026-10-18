import csv
import io
from datetime import date, time, timedelta
from decimal import Decimal as D
from unittest import mock, skipUnless

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import Client, TestCase
from django.urls import reverse

from leave.models import LeaveRequest, LeaveType, PublicHoliday
from payroll import services as payroll_services
from payroll.models import (Approver, Company, CompanyAccess, Employee, EmployeeAdjustment, PayrollRun)

from . import services
from .models import AttendanceRecord, AttendanceSetting, OvertimeApplication, OvertimeRequest

S = AttendanceRecord.Status
WEDNESDAY, SATURDAY = date(2026, 9, 9), date(2026, 9, 12)           # in September 2026


def lines(slip):
    return {l.code: l for l in slip.lines.all()}


class AttCase(TestCase):
    """A real company from seed_demo, so payroll runs go through the actual engine."""

    def setUp(self):
        call_command("seed_demo", verbosity=0)
        self.co = Company.objects.get()
        self.ada = Employee.objects.get(employee_no="E001")
        self.ada.gross_monthly = D("500000")
        self.ada.save()
        self.tunde = Employee.objects.get(employee_no="E002")
        users = get_user_model().objects
        self.admin = users.create_superuser("admin", password="x")
        self.maker = users.create_user("maker")
        self.checker = users.create_user("checker")
        for user in (self.maker, self.checker):
            CompanyAccess.objects.create(user=user, company=self.co)
        # round figures: 500,000 / 160 hours = 3,125 an hour
        AttendanceSetting.objects.create(company=self.co, hours_per_month=D("160"), base_percent=D("100"),
                                         weekday_rate=D("1.5"), weekend_rate=D("2"), holiday_rate=D("2.5"))
        self.client = Client()
        self.client.force_login(self.admin)

    def run_month(self, year, month, final=True):
        run = PayrollRun.objects.create(company=self.co, year=year, month=month)
        payroll_services.generate(run)
        if final:
            run.status = "APPROVED"
            run.save()
        return run

    def regenerate(self, year, month):
        run = PayrollRun.objects.get(company=self.co, year=year, month=month)
        payroll_services.generate(run)
        return run

    def slip(self, run, employee=None):
        return run.payslips.get(employee=employee or self.ada)

    def overtime(self, day=WEDNESDAY, hours="4", approve=True, employee=None, by=None):
        req = services.add_request(employee or self.ada, day, D(hours), "Month end", self.maker)
        if approve:
            services.decide(req, "approve", by or self.checker)
            req.refresh_from_db()
        return req

    def mark(self, day, status, employee=None):
        return AttendanceRecord.objects.create(company=self.co, employee=employee or self.ada, date=day, status=status)


class PricingTests(AttCase):
    def test_unsaved_company_uses_the_defaults(self):
        other = Company.objects.create(name="Other")
        setting = services.settings_for(other)
        self.assertIsNone(setting.pk)
        self.assertEqual((setting.hours_per_month, setting.weekday_rate, setting.base_percent),
                         (D("173.33"), D("1.5"), D("100")))
        self.assertEqual(services.settings_for(self.co).hours_per_month, D("160"))

    def test_day_types(self):
        self.assertEqual(services.day_type(self.co, WEDNESDAY), "WEEKDAY")
        self.assertEqual(services.day_type(self.co, SATURDAY), "WEEKEND")
        PublicHoliday.objects.create(company=self.co, date=WEDNESDAY, name="Holiday")
        PublicHoliday.objects.create(company=self.co, date=SATURDAY, name="Holiday on a weekend")
        self.assertEqual(services.day_type(self.co, WEDNESDAY), "HOLIDAY")
        self.assertEqual(services.day_type(self.co, SATURDAY), "HOLIDAY")             # a holiday outranks the weekend

    def test_hourly_rate(self):
        setting = services.settings_for(self.co)
        self.assertEqual(services.hourly_rate(self.ada, 2026, 9, setting), D("3125.00"))
        setting.base_percent = D("40")
        self.assertEqual(services.hourly_rate(self.ada, 2026, 9, setting), D("1250.00"))   # on basic pay only

    def test_request_rules(self):
        add = lambda day=WEDNESDAY, hours="2": services.add_request(self.ada, day, D(hours), "", self.maker)
        with self.assertRaises(ValueError):
            add(day=date.today() + timedelta(days=1))                                  # cannot be worked yet
        for bad in ("0", "-1", "12.5"):
            with self.assertRaises(ValueError):
                add(hours=bad)
        add(hours="8")
        add(hours="4")                                                                # exactly 12 in the day
        with self.assertRaises(ValueError) as ctx:
            add(hours="0.25")
        self.assertIn("at most 12", str(ctx.exception))
        gone = Employee.objects.create(company=self.co, employee_no="E9", first_name="Gone", last_name="Away",
                                       gross_monthly=D("1"), is_active=False)
        with self.assertRaises(ValueError):
            services.add_request(gone, WEDNESDAY, D("1"), "", self.maker)

    def test_rejected_requests_do_not_count_against_the_day(self):
        req = services.add_request(self.ada, WEDNESDAY, D("10"), "", self.maker)
        services.decide(req, "reject", self.checker)
        services.add_request(self.ada, WEDNESDAY, D("10"), "", self.maker)             # room again

    def test_approval_prices_each_kind_of_day(self):
        PublicHoliday.objects.create(company=self.co, date=date(2026, 9, 10), name="Holiday")
        weekday = self.overtime(WEDNESDAY, "4")
        weekend = self.overtime(SATURDAY, "4")
        holiday = self.overtime(date(2026, 9, 10), "4")
        self.assertEqual((weekday.day_type, weekday.hourly_rate, weekday.multiplier, weekday.amount),
                         ("WEEKDAY", D("3125.00"), D("1.5"), D("18750.00")))
        self.assertEqual((weekend.multiplier, weekend.amount), (D("2"), D("25000.00")))
        self.assertEqual((holiday.day_type, holiday.multiplier, holiday.amount), ("HOLIDAY", D("2.5"), D("31250.00")))

    def test_the_agreed_amount_is_fixed_at_approval(self):
        req = self.overtime()
        AttendanceSetting.objects.filter(company=self.co).update(weekday_rate=D("3"))
        self.ada.gross_monthly = D("900000")
        self.ada.save()
        req.refresh_from_db()
        self.assertEqual(req.amount, D("18750.00"))


class ApprovalTests(AttCase):
    def test_a_different_user_must_decide(self):
        req = services.add_request(self.ada, WEDNESDAY, D("2"), "", self.maker)
        with self.assertRaises(ValueError) as ctx:
            services.decide(req, "approve", self.maker)
        self.assertIn("different user", str(ctx.exception))
        services.decide(req, "approve", self.admin)                                   # administrators may
        self.assertEqual(OvertimeRequest.objects.get().status, "APPROVED")

    def test_designated_approvers_only(self):
        Approver.objects.create(company=self.co, user=self.checker, scope="OVERTIME")
        other = get_user_model().objects.create_user("other")
        CompanyAccess.objects.create(user=other, company=self.co)
        req = services.add_request(self.ada, WEDNESDAY, D("2"), "", self.maker)
        with self.assertRaises(ValueError) as ctx:
            services.decide(req, "approve", other)
        self.assertIn("Only checker can approve overtime requests", str(ctx.exception))
        services.decide(req, "approve", self.checker)

    def test_only_pending_requests_can_be_decided_and_unknown_actions_are_refused(self):
        req = self.overtime()
        for action in ("approve", "reject"):
            with self.assertRaises(ValueError):
                services.decide(req, action, self.admin)
        with self.assertRaises(ValueError):
            services.decide(req, "delete", self.admin)

    def test_cancelling(self):
        pending = services.add_request(self.ada, WEDNESDAY, D("2"), "", self.maker)
        services.decide(pending, "cancel", self.maker)                                # the requester may withdraw
        self.assertEqual(pending.status, "CANCELLED")
        with self.assertRaises(ValueError):
            services.decide(pending, "cancel", self.admin)                            # already closed
        approved = self.overtime()
        Approver.objects.create(company=self.co, user=self.checker, scope="OVERTIME")
        with self.assertRaises(ValueError):
            services.decide(approved, "cancel", self.maker)                           # only designated approvers may
        services.decide(approved, "cancel", self.checker)
        self.assertEqual(approved.status, "CANCELLED")


class OvertimePayTests(AttCase):
    def test_approved_overtime_is_paid_once_and_taxed_on_top(self):
        baseline = self.slip(self.run_month(2026, 9, final=False))
        first, second = self.overtime(WEDNESDAY, "4"), self.overtime(SATURDAY, "2")      # 18,750 + 12,500
        slip = self.slip(self.regenerate(2026, 9))
        pay = lines(slip)["OVERTIME"]
        self.assertEqual((pay.amount, pay.kind), (D("31250.00"), "EARNING"))
        self.assertEqual(pay.name, "Overtime (6 hrs)")
        self.assertEqual(slip.total_earnings - baseline.total_earnings, D("31250.00"))
        self.assertGreater(slip.paye, baseline.paye)
        self.assertEqual(set(OvertimeApplication.objects.filter(payslip=slip).values_list("request_id", flat=True)),
                         {first.pk, second.pk})
        slip = self.slip(self.regenerate(2026, 9))                                    # regenerating does not double it
        self.assertEqual(lines(slip)["OVERTIME"].amount, D("31250.00"))
        self.assertEqual(OvertimeApplication.objects.count(), 2)

    def test_it_is_not_paid_again_in_the_next_month(self):
        self.overtime()
        self.assertIn("OVERTIME", lines(self.slip(self.run_month(2026, 9))))
        self.assertNotIn("OVERTIME", lines(self.slip(self.run_month(2026, 10))))

    def test_only_approved_requests_are_paid(self):
        self.overtime(approve=False)                                                  # waiting
        rejected = services.add_request(self.ada, WEDNESDAY, D("1"), "", self.maker)
        services.decide(rejected, "reject", self.checker)
        cancelled = self.overtime(day=SATURDAY)
        Approver.objects.create(company=self.co, user=self.checker, scope="OVERTIME")
        services.decide(cancelled, "cancel", self.checker)
        self.assertNotIn("OVERTIME", lines(self.slip(self.run_month(2026, 9))))

    def test_late_approval_is_picked_up_by_the_next_run(self):
        req = services.add_request(self.ada, date(2026, 9, 21), D("4"), "", self.maker)   # a Monday in September
        self.assertNotIn("OVERTIME", lines(self.slip(self.run_month(2026, 9))))          # September closes without it
        services.decide(req, "approve", self.checker)
        october = self.slip(self.run_month(2026, 10))
        self.assertEqual(lines(october)["OVERTIME"].amount, D("18750.00"))

    def test_overtime_for_a_later_month_waits_for_its_own_run(self):
        self.overtime(day=date(2026, 9, 28))
        self.assertNotIn("OVERTIME", lines(self.slip(self.run_month(2026, 8, final=False))))

    def test_the_amount_paid_is_the_amount_approved(self):
        self.overtime()
        AttendanceSetting.objects.filter(company=self.co).update(weekday_rate=D("3"))
        self.assertEqual(lines(self.slip(self.run_month(2026, 9)))["OVERTIME"].amount, D("18750.00"))

    def test_paid_overtime_cannot_be_cancelled(self):
        req = self.overtime()
        Approver.objects.create(company=self.co, user=self.checker, scope="OVERTIME")
        self.run_month(2026, 9)
        self.assertTrue(services.is_paid(req))
        with self.assertRaises(ValueError) as ctx:
            services.decide(req, "cancel", self.checker)
        self.assertIn("paid in an approved payroll run", str(ctx.exception))

    def test_an_unapproved_draft_does_not_make_it_paid(self):
        req = self.overtime()
        self.run_month(2026, 9, final=False)
        self.assertFalse(services.is_paid(req))

    @skipUnless(apps.is_installed("exits"), "the exits module is not installed")
    def test_overtime_is_paid_in_the_final_month_of_a_leaver(self):
        from exits import services as exit_services
        self.overtime()
        exit_services.start_exit(self.ada, "RESIGNATION", date(2026, 9, 30))
        self.assertEqual(lines(self.slip(self.run_month(2026, 9)))["OVERTIME"].amount, D("18750.00"))


class AbsenceTests(AttCase):
    def test_absent_days_reduce_pay_at_the_usual_daily_rate(self):
        self.mark(date(2026, 9, 7), S.ABSENT)
        self.mark(date(2026, 9, 8), S.ABSENT)
        self.mark(date(2026, 9, 9), S.HALF_DAY)
        self.mark(date(2026, 9, 10), S.LATE)
        self.mark(date(2026, 9, 11), S.PRESENT)
        self.mark(date(2026, 8, 31), S.ABSENT)                                         # another month: not counted
        slip = self.slip(self.run_month(2026, 9))
        absence = lines(slip)["ABSENCE"]
        self.assertEqual(absence.amount, D("41666.67"))                               # 2.5 days at 500,000 / 30
        self.assertIn("2.5 day(s)", absence.name)

    def test_nothing_is_taken_when_everyone_was_present_or_late(self):
        self.mark(date(2026, 9, 7), S.PRESENT)
        self.mark(date(2026, 9, 8), S.LATE)
        self.assertNotIn("ABSENCE", lines(self.slip(self.run_month(2026, 9))))

    def test_days_already_covered_by_leave_or_a_holiday_are_not_counted_twice(self):
        annual = LeaveType.objects.create(company=self.co, name="Unpaid break", days_per_year=0, is_paid=True)
        LeaveRequest.objects.create(company=self.co, employee=self.ada, leave_type=annual, days=1,
                                    start_date=date(2026, 9, 7), end_date=date(2026, 9, 7), status="APPROVED")
        PublicHoliday.objects.create(company=self.co, date=date(2026, 9, 8), name="Holiday")
        self.mark(date(2026, 9, 7), S.ABSENT)                                          # on approved leave
        self.mark(date(2026, 9, 8), S.ABSENT)                                          # public holiday
        self.mark(date(2026, 9, 9), S.ABSENT)                                          # a real absence
        self.assertEqual(lines(self.slip(self.run_month(2026, 9)))["ABSENCE"].amount, D("16666.67"))   # one day

    def test_it_adds_to_other_absence_in_a_single_line(self):
        EmployeeAdjustment.objects.create(company=self.co, employee=self.ada, kind="ABSENCE", description="Away",
                                          start_year=2026, start_month=9, days=3)
        self.mark(date(2026, 9, 7), S.ABSENT)
        self.mark(date(2026, 9, 8), S.ABSENT)
        absence = lines(self.slip(self.run_month(2026, 9)))["ABSENCE"]
        self.assertEqual(absence.amount, D("83333.33"))                               # 5 days
        self.assertIn("5 day(s)", absence.name)

    def test_without_the_leave_module_absence_still_counts(self):
        self.mark(date(2026, 9, 7), S.ABSENT)
        with mock.patch("attendance.services._has_leave", return_value=False):
            self.assertEqual(services.absent_days(self.ada, PayrollRun(year=2026, month=9)), D("1"))
            self.assertEqual(services.day_type(self.co, WEDNESDAY), "WEEKDAY")

    def test_other_employees_are_unaffected(self):
        self.mark(date(2026, 9, 7), S.ABSENT)
        self.assertNotIn("ABSENCE", lines(self.slip(self.run_month(2026, 9), self.tunde)))


class RegisterTests(AttCase):
    def url(self, day=WEDNESDAY, **extra):
        query = "".join(f"&{k}={v}" for k, v in extra.items())
        return f"{reverse('attendance:register')}?date={day.isoformat()}{query}"

    def post(self, rows, day=WEDNESDAY):
        data = {"date": day.isoformat()}
        for emp, (status, time_in, time_out, note) in rows.items():
            data.update({f"status_{emp.pk}": status, f"in_{emp.pk}": time_in, f"out_{emp.pk}": time_out,
                         f"note_{emp.pk}": note})
        return self.client.post(reverse("attendance:register"), data)

    def test_the_page_lists_active_staff_and_the_kind_of_day(self):
        page = self.client.get(self.url(SATURDAY))
        self.assertContains(page, "Weekend")
        self.assertContains(page, "E001")
        self.assertEqual([r["emp"] for r in page.context["rows"]],
                         list(Employee.objects.filter(company=self.co, is_active=True).order_by("employee_no")))

    def test_saving_creates_updates_and_clears(self):
        resp = self.post({self.ada: ("PRESENT", "08:05", "17:00", "Early"), self.tunde: ("ABSENT", "", "", "")})
        self.assertRedirects(resp, self.url(), fetch_redirect_response=False)
        ada = AttendanceRecord.objects.get(employee=self.ada)
        self.assertEqual((ada.status, ada.time_in, ada.time_out, ada.note, ada.recorded_by),
                         ("PRESENT", time(8, 5), time(17, 0), "Early", self.admin))
        self.assertEqual(ada.hours, D("8.92"))
        self.post({self.ada: ("LATE", "09:30", "", ""), self.tunde: ("", "", "", "")})
        ada.refresh_from_db()
        self.assertEqual((ada.status, ada.time_in, ada.time_out), ("LATE", time(9, 30), None))
        self.assertFalse(AttendanceRecord.objects.filter(employee=self.tunde).exists())          # cleared
        self.assertEqual(AttendanceRecord.objects.count(), 1)

    def test_saved_values_come_back_on_the_page(self):
        self.mark(WEDNESDAY, S.LATE)
        row = [r for r in self.client.get(self.url()).context["rows"] if r["emp"] == self.ada][0]
        self.assertEqual(row["v"]["status"], "LATE")

    def test_one_bad_row_saves_nothing(self):
        resp = self.post({self.ada: ("PRESENT", "", "", ""), self.tunde: ("PRESENT", "17:00", "08:00", "")})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "time out must be after the time in")
        self.assertContains(resp, "Nothing was saved")
        self.assertFalse(AttendanceRecord.objects.exists())
        bad_time = self.post({self.ada: ("PRESENT", "late", "", "")})
        self.assertContains(bad_time, "is not a time")
        bad_status = self.post({self.ada: ("TELEPORTED", "", "", "")})
        self.assertContains(bad_status, "Choose a status")
        self.assertFalse(AttendanceRecord.objects.exists())

    def test_the_future_cannot_be_filled_in(self):
        tomorrow = date.today() + timedelta(days=1)
        resp = self.post({self.ada: ("PRESENT", "", "", "")}, day=tomorrow)
        self.assertContains(resp, "has not happened yet")
        self.assertFalse(AttendanceRecord.objects.exists())

    def test_prefill_marks_blanks_present_except_people_on_leave(self):
        annual = LeaveType.objects.create(company=self.co, name="Annual", days_per_year=20, is_paid=True)
        LeaveRequest.objects.create(company=self.co, employee=self.tunde, leave_type=annual, days=1,
                                    start_date=WEDNESDAY, end_date=WEDNESDAY, status="APPROVED")
        self.mark(WEDNESDAY, S.LATE)                                                   # Ada already has a status
        rows = {r["emp"]: r for r in self.client.get(self.url(fill="present")).context["rows"]}
        self.assertEqual(rows[self.ada]["v"]["status"], "LATE")
        self.assertEqual(rows[self.tunde]["v"]["status"], "")
        self.assertContains(self.client.get(self.url()), "On annual")
        self.assertFalse(AttendanceRecord.objects.filter(employee=self.tunde).exists())          # nothing saved by a GET

    def test_other_companies_staff_cannot_be_written_to(self):
        other = Company.objects.create(name="Beta")
        stranger = Employee.objects.create(company=other, employee_no="Z1", first_name="Zed", last_name="Zulu",
                                           gross_monthly=D("1"))
        self.client.post(reverse("attendance:register"), {"date": WEDNESDAY.isoformat(),
                                                           f"status_{stranger.pk}": "ABSENT"})
        self.assertFalse(AttendanceRecord.objects.filter(employee=stranger).exists())

    def test_login_is_required(self):
        resp = Client().get(reverse("attendance:register"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("login", resp["Location"])


class ImportTests(AttCase):
    def upload(self, text):
        return self.client.post(reverse("attendance:import"), {
            "file": SimpleUploadedFile("attendance.csv", text.encode(), content_type="text/csv")}, follow=True)

    def test_importing_creates_and_replaces(self):
        text = ("employee_no,date,status,time_in,time_out,note\n"
                "E001,2026-09-07,present,08:00,17:00,\n"
                "E002,08/09/2026,Absent,,,sick\n"
                "E001,2026-09-09,L,09:30,,\n")
        self.assertContains(self.upload(text), "3 new record(s), 0 updated")
        self.assertEqual(AttendanceRecord.objects.count(), 3)
        sick = AttendanceRecord.objects.get(employee=self.tunde)
        self.assertEqual((sick.date, sick.status, sick.note), (date(2026, 9, 8), "ABSENT", "sick"))
        self.assertEqual(AttendanceRecord.objects.get(employee=self.ada, date=date(2026, 9, 9)).status, "LATE")
        self.assertContains(self.upload("employee_no,date,status\nE001,2026-09-07,absent\n"), "0 new record(s), 1 updated")
        self.assertEqual(AttendanceRecord.objects.get(employee=self.ada, date=date(2026, 9, 7)).status, "ABSENT")

    def test_one_bad_row_imports_nothing_and_every_problem_is_listed(self):
        text = ("employee_no,date,status,time_in,time_out\n"
                "E001,2026-09-07,present,,\n"
                "E404,2026-09-07,present,,\n"
                "E001,not-a-date,present,,\n"
                "E001,2026-09-08,sleeping,,\n"
                "E001,2026-09-10,present,17:00,08:00\n"
                f"E001,{(date.today() + timedelta(days=3)).isoformat()},present,,\n")
        resp = self.upload(text)
        for expected in ("Nothing was imported", "Line 3: no active employee numbered", "E404", "Line 4:",
                         "Line 5:", "is not a status", "time out must be after", "in the future"):
            self.assertContains(resp, expected)
        self.assertFalse(AttendanceRecord.objects.exists())

    def test_files_that_cannot_be_used_are_explained(self):
        self.assertContains(self.upload("employee_no,date\nE001,2026-09-07\n"), "Missing: status")
        self.assertContains(self.upload(""), "submitted file is empty")                 # Django stops empty uploads
        self.assertContains(self.upload("\n"), "needs these columns")
        self.assertFalse(AttendanceRecord.objects.exists())
        binary = self.client.post(reverse("attendance:import"), {"file": SimpleUploadedFile("x.csv", b"\xff\xfe\x00\x01")})
        self.assertContains(binary, "not plain text")
        big = SimpleUploadedFile("big.csv", b"x" * (1024 * 1024 + 1))
        self.assertContains(self.client.post(reverse("attendance:import"), {"file": big}), "larger than 1 MB")

    def test_other_companies_and_former_staff_are_not_found(self):
        other = Company.objects.create(name="Beta")
        Employee.objects.create(company=other, employee_no="Z1", first_name="Zed", last_name="Zulu", gross_monthly=D("1"))
        gone = Employee.objects.create(company=self.co, employee_no="E9", first_name="Gone", last_name="Away",
                                       gross_monthly=D("1"), is_active=False)
        resp = self.upload("employee_no,date,status\nZ1,2026-09-07,present\nE9,2026-09-07,present\n")
        self.assertContains(resp, "Line 2: no active employee numbered")
        self.assertContains(resp, "Line 3: no active employee numbered")
        self.assertContains(resp, "Z1")
        self.assertContains(resp, "E9")
        self.assertFalse(AttendanceRecord.objects.exists())


class SummaryTests(AttCase):
    def test_counts_overtime_and_leave_by_employee(self):
        for day, status in ((date(2026, 9, 7), S.PRESENT), (date(2026, 9, 8), S.PRESENT), (date(2026, 9, 9), S.LATE),
                            (date(2026, 9, 10), S.ABSENT), (date(2026, 9, 11), S.HALF_DAY)):
            self.mark(day, status)
        self.mark(date(2026, 8, 31), S.ABSENT)                                          # not September
        self.overtime(WEDNESDAY, "4")
        self.overtime(SATURDAY, "2", approve=False)                                    # pending: not counted
        annual = LeaveType.objects.create(company=self.co, name="Annual", days_per_year=20, is_paid=True)
        LeaveRequest.objects.create(company=self.co, employee=self.ada, leave_type=annual, days=2,
                                    start_date=date(2026, 9, 14), end_date=date(2026, 9, 15), status="APPROVED")
        page = self.client.get(reverse("attendance:summary"), {"month": "2026-09"})
        row = [r for r in page.context["rows"] if r["emp"] == self.ada][0]
        self.assertEqual((row["present"], row["late"], row["absent"], row["half"], row["leave_days"]),
                         (2, 1, 1, 1, D("2")))
        self.assertEqual((row["ot_hours"], row["ot_amount"]), (D("4"), D("18750.00")))
        self.assertContains(page, "September 2026")
        self.assertEqual(page.context["ot_amount"], D("18750.00"))

    def test_leave_days_skip_public_holidays(self):
        annual = LeaveType.objects.create(company=self.co, name="Annual", days_per_year=20, is_paid=True)
        PublicHoliday.objects.create(company=self.co, date=date(2026, 9, 15), name="Holiday")
        LeaveRequest.objects.create(company=self.co, employee=self.ada, leave_type=annual, days=1,
                                    start_date=date(2026, 9, 14), end_date=date(2026, 9, 15), status="APPROVED")
        row = [r for r in services.month_summary(self.co, 2026, 9) if r["emp"] == self.ada][0]
        self.assertEqual(row["leave_days"], D("1"))

    def test_csv_export(self):
        self.ada.first_name = "=HYPERLINK(\"http://evil\")"
        self.ada.save()
        self.mark(date(2026, 9, 7), S.ABSENT)
        resp = self.client.get(reverse("attendance:summary_csv"), {"month": "2026-09"})
        self.assertIn("attachment", resp["Content-Disposition"])
        rows = list(csv.reader(io.StringIO(resp.content.decode())))
        self.assertEqual(rows[0][:5], ["Employee no", "Name", "Present", "Late", "Absent"])
        ada = [r for r in rows if r[0] == "E001"][0]
        self.assertTrue(ada[1].startswith("'="))                                        # formula defused
        self.assertEqual(ada[4], "1")

    def test_month_navigation_and_bad_input(self):
        page = self.client.get(reverse("attendance:summary"), {"month": "2026-09"})
        self.assertEqual(page.context["prev"], date(2026, 8, 1))
        self.assertEqual(self.client.get(reverse("attendance:summary"), {"month": "garbage"}).status_code, 200)
        this_month = date.today().replace(day=1)
        self.assertIsNone(self.client.get(reverse("attendance:summary"),
                                          {"month": this_month.strftime("%Y-%m")}).context["next"])


class OvertimePagesTests(AttCase):
    def test_recording_through_the_page(self):
        resp = self.client.post(reverse("attendance:overtime_new"), {
            "employee": self.ada.pk, "date": WEDNESDAY.isoformat(), "hours": "2.5", "reason": "Stock count"})
        req = OvertimeRequest.objects.get()
        self.assertRedirects(resp, reverse("attendance:overtime_detail", args=[req.pk]))
        self.assertEqual((req.hours, req.status, req.requested_by, req.company),
                         (D("2.50"), "PENDING", self.admin, self.co))
        page = self.client.get(reverse("attendance:overtime_detail", args=[req.pk]))
        self.assertContains(page, "If approved now")
        self.assertContains(page, "3,125.00")
        self.assertContains(page, "11,718.75")                                         # 3,125 x 1.5 x 2.5

    def test_invalid_requests_are_explained(self):
        url = reverse("attendance:overtime_new")
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        future = self.client.post(url, {"employee": self.ada.pk, "date": tomorrow, "hours": "2"})
        self.assertContains(future, "already happened")
        self.assertEqual(self.client.post(url, {"employee": self.ada.pk, "date": WEDNESDAY.isoformat(),
                                                "hours": "0"}).status_code, 200)
        self.assertFalse(OvertimeRequest.objects.exists())

    def test_deciding_through_the_page(self):
        req = services.add_request(self.ada, WEDNESDAY, D("4"), "", self.maker)
        resp = self.client.post(reverse("attendance:overtime_decide", args=[req.pk, "approve"]),
                                {"note": "Fine"}, follow=True)
        self.assertContains(resp, "Overtime approved")
        req.refresh_from_db()
        self.assertEqual((req.status, req.decided_by, req.decision_note, req.amount),
                         ("APPROVED", self.admin, "Fine", D("18750.00")))
        self.assertEqual(self.client.get(reverse("attendance:overtime_decide", args=[req.pk, "cancel"])).status_code, 405)
        bad = self.client.post(reverse("attendance:overtime_decide", args=[req.pk, "explode"]), follow=True)
        self.assertContains(bad, "Unknown action")

    def test_a_user_who_may_not_approve_is_told_so(self):
        Approver.objects.create(company=self.co, user=self.checker, scope="OVERTIME")
        req = services.add_request(self.ada, WEDNESDAY, D("4"), "", self.maker)
        staff = Client()
        staff.force_login(self.maker)
        resp = staff.post(reverse("attendance:overtime_decide", args=[req.pk, "approve"]), follow=True)
        self.assertContains(resp, "Only checker can approve")
        self.assertEqual(OvertimeRequest.objects.get().status, "PENDING")
        self.assertContains(staff.get(reverse("attendance:overtime_detail", args=[req.pk])), "Waiting for checker")

    def test_list_filter_and_menu(self):
        services.add_request(self.ada, WEDNESDAY, D("1"), "", self.maker)
        self.overtime(SATURDAY, "2")
        page = self.client.get(reverse("attendance:overtime"), {"status": "APPROVED"})
        self.assertEqual([r.status for r in page.context["requests"]], ["APPROVED"])
        self.assertContains(page, ">Attendance</a>")
        self.assertEqual(len(self.client.get(reverse("attendance:overtime")).context["requests"]), 2)

    def test_approvers_page_offers_the_overtime_scope(self):
        self.assertContains(self.client.get(reverse("approver_list")), "Overtime requests")

    def test_other_companies_are_unreachable(self):
        other = Company.objects.create(name="Beta")
        stranger = Employee.objects.create(company=other, employee_no="Z1", first_name="Zed", last_name="Zulu",
                                           gross_monthly=D("1"))
        theirs = OvertimeRequest.objects.create(company=other, employee=stranger, date=WEDNESDAY, hours=D("1"))
        self.assertEqual(self.client.get(reverse("attendance:overtime_detail", args=[theirs.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("attendance:overtime_decide", args=[theirs.pk, "approve"])).status_code, 404)
        staff = Client()
        staff.force_login(self.maker)
        self.assertNotContains(staff.get(reverse("attendance:overtime")), "Zed")
        form = self.client.get(reverse("attendance:overtime_new")).context["form"]
        self.assertNotIn(stranger, set(form.fields["employee"].queryset))

    def test_rules_page_is_for_administrators_and_validated(self):
        staff = Client()
        staff.force_login(self.maker)
        self.assertEqual(staff.get(reverse("attendance:settings")).status_code, 302)
        url = reverse("attendance:settings")
        good = {"standard_daily_hours": "8", "hours_per_month": "176", "base_percent": "60", "weekday_rate": "1.5",
                "weekend_rate": "2", "holiday_rate": "3"}
        self.assertRedirects(self.client.post(url, good), reverse("attendance:overtime"), fetch_redirect_response=False)
        saved = AttendanceSetting.objects.get(company=self.co)
        self.assertEqual((saved.hours_per_month, saved.base_percent, saved.holiday_rate), (D("176"), D("60"), D("3")))
        self.assertEqual(AttendanceSetting.objects.count(), 1)                          # updated, not duplicated
        self.assertEqual(self.client.post(url, {**good, "hours_per_month": "0"}).status_code, 200)
        self.assertEqual(AttendanceSetting.objects.get(company=self.co).hours_per_month, D("176"))
        req = self.overtime()
        self.assertEqual(req.hourly_rate, D("1704.55"))                                  # 500,000 x 60% / 176: the new rules apply


@skipUnless(apps.is_installed("audit"), "the audit log is not installed")
class AuditTests(AttCase):
    def test_register_changes_and_decisions_are_traced(self):
        from audit.models import AuditEntry
        self.client.post(reverse("attendance:register"), {"date": WEDNESDAY.isoformat(),
                                                           f"status_{self.ada.pk}": "ABSENT"})
        self.client.post(reverse("attendance:register"), {"date": WEDNESDAY.isoformat(),
                                                           f"status_{self.ada.pk}": "PRESENT"})
        change = AuditEntry.objects.get(model_label="attendance.attendancerecord", action="UPDATE")
        self.assertEqual((change.who, change.employee_id), ("admin", self.ada.pk))
        self.assertEqual(change.changes["status"], ["Absent (unpaid)", "Present"])
        req = services.add_request(self.ada, WEDNESDAY, D("2"), "", self.maker)
        self.client.post(reverse("attendance:overtime_decide", args=[req.pk, "approve"]))
        decided = AuditEntry.objects.get(model_label="attendance.overtimerequest", action="UPDATE")
        self.assertEqual(decided.changes["status"], ["Waiting for approval", "Approved"])
