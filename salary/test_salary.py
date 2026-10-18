from datetime import date
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db.models import ProtectedError
from django.test import Client, TestCase
from django.urls import reverse

from payroll import services as payroll_services
from payroll.models import Company, CompanyAccess, Employee, EmployeeAdjustment, Payslip, PayrollRun

from . import services
from .forms import RevisionForm
from .models import ArrearsApplication, PayGrade, SalaryBasis, SalaryRevision

R = SalaryRevision.Reason


def lines(slip):
    return {l.code: l.amount for l in slip.lines.all()}


class SalaryCase(TestCase):
    """A real company from seed_demo, so payroll runs go through the actual engine."""

    def setUp(self):
        call_command("seed_demo", verbosity=0)
        self.co = Company.objects.get()
        self.ada = Employee.objects.get(employee_no="E001")
        self.ada.gross_monthly = D("500000")
        self.ada.save()
        users = get_user_model().objects
        self.admin = users.create_superuser("admin", password="x")
        self.client = Client()
        self.client.force_login(self.admin)

    def run_month(self, year, month, final=True):
        run = PayrollRun.objects.create(company=self.co, year=year, month=month)
        payroll_services.generate(run)
        if final:
            run.status = "APPROVED"
            run.save()
        return run

    def slip(self, run, employee=None):
        return run.payslips.get(employee=employee or self.ada)


class RevisionRuleTests(SalaryCase):
    def test_effective_gross_picks_the_revision_in_force(self):
        a = SalaryRevision(effective_from=date(2026, 1, 1), gross_monthly=D("100"))
        b = SalaryRevision(effective_from=date(2026, 7, 1), gross_monthly=D("200"))
        self.assertEqual(services.effective_gross([a, b], 2026, 6), D("100"))
        self.assertEqual(services.effective_gross([a, b], 2026, 7), D("200"))     # the whole month it starts in
        self.assertEqual(services.effective_gross([a, b], 2027, 1), D("200"))
        self.assertEqual(services.effective_gross([a, b], 2025, 1), D("100"))     # before history: the first figure

    def test_first_revision_fixes_the_opening_salary(self):
        services.add_revision(self.ada, date(2027, 1, 15), D("600000"), R.REVIEW)
        revs = list(self.ada.salary_revisions.all())
        self.assertEqual([(r.effective_from, r.gross_monthly, r.reason) for r in revs],
                         [(date(2026, 12, 1), D("500000.00"), R.OPENING), (date(2027, 1, 1), D("600000.00"), R.REVIEW)])

    def test_future_change_does_not_alter_pay_until_its_month(self):
        services.add_revision(self.ada, date(2027, 1, 1), D("600000"), R.REVIEW)
        self.ada.refresh_from_db()
        self.assertEqual(self.ada.gross_monthly, D("500000.00"))
        self.assertEqual(services.gross_for(self.ada, PayrollRun(year=2026, month=12)), D("500000.00"))
        self.assertEqual(services.gross_for(self.ada, PayrollRun(year=2027, month=1)), D("600000.00"))
        services.sync_current(self.ada, today=date(2027, 1, 20))
        self.ada.refresh_from_db()
        self.assertEqual(self.ada.gross_monthly, D("600000.00"))

    def test_no_history_means_no_override(self):
        self.assertIsNone(services.gross_for(self.ada, PayrollRun(year=2026, month=9)))
        self.assertFalse(services.has_history(self.ada))

    def test_current_gross_is_kept_in_step(self):
        services.add_revision(self.ada, date(2026, 1, 1), D("550000"), R.PROMOTION)
        self.ada.refresh_from_db()
        self.assertEqual(self.ada.gross_monthly, D("550000.00"))

    def test_locking_and_deleting(self):
        self.run_month(2026, 9)                                                    # September is approved
        first = services.add_revision(self.ada, date(2026, 11, 1), D("520000"), R.REVIEW)
        opening = self.ada.salary_revisions.get(reason=R.OPENING)
        self.assertEqual(opening.effective_from, date(2026, 9, 1))                 # rebuilt from the approved payslip
        self.assertTrue(services.is_locked(opening))
        self.assertFalse(services.is_locked(first))                                # October is the first open month
        with self.assertRaises(ValueError):
            services.delete_revision(opening)
        later = services.add_revision(self.ada, date(2026, 12, 1), D("540000"), R.REVIEW)
        services.delete_revision(later)
        self.assertEqual(self.ada.salary_revisions.count(), 2)
        self.run_month(2026, 10)
        self.run_month(2026, 11)
        with self.assertRaises(ValueError):
            services.delete_revision(first)                                        # November is now approved
        self.assertTrue(SalaryRevision.objects.filter(pk=first.pk).exists())

    def test_opening_cannot_be_deleted_while_later_changes_exist(self):
        services.add_revision(self.ada, date(2026, 11, 1), D("520000"), R.REVIEW)
        opening = self.ada.salary_revisions.get(reason=R.OPENING)
        with self.assertRaises(ValueError):
            services.delete_revision(opening)

    def test_first_open_month_follows_the_last_approved_run(self):
        self.assertEqual(services.first_open_month(self.co), date.today().replace(day=1))
        self.run_month(2026, 11)
        self.assertEqual(services.first_open_month(self.co), date(2026, 12, 1))
        self.run_month(2026, 12, final=False)                                      # a draft changes nothing
        self.assertEqual(services.first_open_month(self.co), date(2026, 12, 1))


class FormTests(SalaryCase):
    def form(self, **data):
        base = {"effective_from": "2026-11-15", "gross_monthly": "600000", "reason": "REVIEW"}
        return RevisionForm({**base, **data}, employee=self.ada,
                            instance=SalaryRevision(company=self.co, employee=self.ada))

    def test_valid_form_moves_the_date_to_the_first_of_the_month(self):
        form = self.form()
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["effective_from"], date(2026, 11, 1))

    def test_bad_values(self):
        self.assertFalse(self.form(gross_monthly="0").is_valid())
        self.assertFalse(self.form(gross_monthly="-5").is_valid())
        self.assertFalse(self.form(reason="OPENING").is_valid())                   # system use only

    def test_duplicate_month_and_pre_history_dates_are_refused(self):
        services.add_revision(self.ada, date(2026, 11, 1), D("520000"), R.REVIEW)
        self.assertIn("already has a change", str(self.form(effective_from="2026-11-20").errors))
        self.assertIn("before this employee", str(self.form(effective_from="2020-01-01").errors))

    def test_grade_choices_are_this_companys_only(self):
        other = Company.objects.create(name="Other")
        mine = PayGrade.objects.create(company=self.co, name="G1", min_gross=1, max_gross=9)
        theirs = PayGrade.objects.create(company=other, name="G2", min_gross=1, max_gross=9)
        offered = set(self.form().fields["grade"].queryset)
        self.assertEqual(offered, {mine})
        self.assertNotIn(theirs, offered)


class BackPayTests(SalaryCase):
    def test_backdated_raise_pays_arrears_once_and_taxes_them_as_a_lump_sum(self):
        sept = self.run_month(2026, 9)
        self.assertEqual(SalaryBasis.objects.get(payslip=self.slip(sept)).monthly_gross, D("500000.00"))
        services.add_revision(self.ada, date(2026, 9, 1), D("600000"), R.PROMOTION)
        nxt, pending = services.pending(self.co)
        self.assertEqual(nxt, date(2026, 10, 1))
        self.assertEqual([(p["employee"], p["owed"]) for p in pending], [(self.ada, D("100000.00"))])

        october = self.run_month(2026, 10)
        oct_slip = self.slip(october)
        self.assertEqual(oct_slip.gross, D("600000.00"))
        self.assertEqual(lines(oct_slip)["ARREARS"], D("100000.00"))
        self.assertEqual(ArrearsApplication.objects.get(payslip=oct_slip).amount, D("100000.00"))

        november = self.run_month(2026, 11)                                        # nothing left to pay
        nov_slip = self.slip(november)
        self.assertNotIn("ARREARS", lines(nov_slip))
        self.assertEqual(nov_slip.gross, D("600000.00"))
        self.assertGreater(oct_slip.paye, nov_slip.paye)                           # lump sum taxed on top, once
        self.assertEqual(services.pending(self.co)[1], [])

    def test_regenerating_a_draft_does_not_pay_arrears_twice(self):
        self.run_month(2026, 9)
        services.add_revision(self.ada, date(2026, 9, 1), D("600000"), R.PROMOTION)
        october = self.run_month(2026, 10, final=False)
        payroll_services.generate(october)
        payroll_services.generate(october)
        slip = self.slip(october)
        self.assertEqual(lines(slip)["ARREARS"], D("100000.00"))
        self.assertEqual(ArrearsApplication.objects.filter(payslip=slip).count(), 1)

    def test_a_second_correction_only_pays_the_difference(self):
        self.run_month(2026, 9)
        services.add_revision(self.ada, date(2026, 9, 1), D("600000"), R.PROMOTION)
        self.run_month(2026, 10)                                                   # pays 100,000
        services.add_revision(self.ada, date(2026, 10, 1), D("650000"), R.REVIEW)  # October should have been 650,000
        owed, _, _ = services.arrears_for(self.ada, self.co, 2026, 11)
        self.assertEqual(owed, D("50000.00"))                                      # not 150,000

    def test_earlier_raises_do_not_create_false_back_pay(self):
        """Paid 400k in June and July, 500k after. Recording a later raise must not claim 100k for June or July."""
        for month, gross in ((6, "400000"), (7, "400000"), (8, "500000"), (9, "500000")):
            self.ada.gross_monthly = D(gross)
            self.ada.save()
            self.run_month(2026, month)
        services.add_revision(self.ada, date(2026, 11, 1), D("700000"), R.PROMOTION)
        self.assertEqual([(r.effective_from, r.gross_monthly) for r in self.ada.salary_revisions.order_by("effective_from")],
                         [(date(2026, 6, 1), D("400000.00")), (date(2026, 8, 1), D("500000.00")),
                          (date(2026, 11, 1), D("700000.00"))])
        self.assertEqual(services.arrears_for(self.ada, self.co, 2026, 10)[0], D("0.00"))
        self.assertEqual(self.slip(self.run_month(2026, 10)).gross, D("500000.00"))
        self.assertEqual(self.slip(self.run_month(2026, 11)).gross, D("700000.00"))

    def test_backdating_before_older_payslips_corrects_only_from_that_month(self):
        for month in (7, 8, 9):
            self.run_month(2026, month)
        services.add_revision(self.ada, date(2026, 8, 1), D("560000"), R.CORRECTION)
        owed, applied, _ = services.arrears_for(self.ada, self.co, 2026, 10)
        self.assertEqual(owed, D("120000.00"))                                     # August and September, 60k each
        self.assertEqual(len(applied), 2)

    def test_overpayment_is_reported_but_never_deducted(self):
        self.run_month(2026, 9)
        before = self.slip(self.run_month(2026, 10, final=False)).net_pay
        services.add_revision(self.ada, date(2026, 9, 1), D("450000"), R.CORRECTION)
        owed, _, over = services.arrears_for(self.ada, self.co, 2026, 10)
        self.assertEqual((owed, over), (D("0.00"), D("50000.00")))
        october = PayrollRun.objects.get(year=2026, month=10)
        payroll_services.generate(october)
        slip = self.slip(october)
        self.assertNotIn("ARREARS", lines(slip))
        self.assertEqual(slip.gross, D("450000.00"))                               # the new salary applies going forward
        self.assertLess(slip.net_pay, before)

    def test_basis_for_old_payslips_adds_back_absence(self):
        EmployeeAdjustment.objects.create(company=self.co, employee=self.ada, kind="ABSENCE", description="Away",
                                          start_year=2026, start_month=9, days=3)
        sept = self.run_month(2026, 9)
        slip = self.slip(sept)
        self.assertLess(slip.gross, D("500000"))                                   # pay was reduced
        SalaryBasis.objects.filter(payslip=slip).delete()                          # as if made before this module
        services.backfill_basis(self.ada)
        self.assertEqual(SalaryBasis.objects.get(payslip=slip).monthly_gross, D("500000.00"))

    def test_absence_still_reduces_the_new_salary(self):
        services.add_revision(self.ada, date(2026, 9, 1), D("600000"), R.REVIEW)
        EmployeeAdjustment.objects.create(company=self.co, employee=self.ada, kind="ABSENCE", description="Away",
                                          start_year=2026, start_month=10, days=3)
        slip = self.slip(self.run_month(2026, 10))
        self.assertEqual(slip.gross, D("600000") - D("600000") / 30 * 3)
        self.assertEqual(SalaryBasis.objects.get(payslip=slip).monthly_gross, D("600000.00"))

    def test_employees_without_history_are_untouched(self):
        slip = self.slip(self.run_month(2026, 9), Employee.objects.get(employee_no="E002"))
        self.assertEqual(slip.gross, Employee.objects.get(employee_no="E002").gross_monthly)


class EmployeeFormLockTests(SalaryCase):
    def post_gross(self, value):
        url = reverse("section_edit", args=["employees", self.ada.pk])
        return self.client.post(url, {
            "employee_no": "E001", "first_name": self.ada.first_name, "last_name": self.ada.last_name,
            "gross_monthly": value, "annual_rent": "0", "pension_applies": "on", "is_active": "on"})

    def test_editable_while_there_is_no_history(self):
        self.post_gross("510000")
        self.ada.refresh_from_db()
        self.assertEqual(self.ada.gross_monthly, D("510000.00"))

    def test_locked_once_history_exists(self):
        services.add_revision(self.ada, date(2026, 1, 1), D("550000"), R.REVIEW)
        page = self.client.get(reverse("section_edit", args=["employees", self.ada.pk]))
        self.assertContains(page, "Change the salary on the Salary page")
        self.post_gross("999999")
        self.ada.refresh_from_db()
        self.assertEqual(self.ada.gross_monthly, D("550000.00"))                   # the posted value was ignored


class ViewTests(SalaryCase):
    def test_login_required(self):
        resp = Client().get(reverse("salary:overview"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("login", resp["Location"])

    def test_menu_and_overview(self):
        resp = self.client.get(reverse("salary:overview"))
        self.assertContains(resp, ">Salary</a>")
        self.assertContains(resp, "E001")
        self.assertContains(resp, "500,000.00")

    def test_new_change_through_the_page_with_back_pay_message(self):
        self.run_month(2026, 9)
        resp = self.client.post(reverse("salary:revision_new", args=[self.ada.pk]), {
            "effective_from": "2026-09-01", "gross_monthly": "600000", "reason": "PROMOTION", "note": "Senior"},
            follow=True)
        self.assertRedirects(resp, reverse("salary:detail", args=[self.ada.pk]))
        self.assertContains(resp, "Back pay of")
        self.assertContains(resp, "100,000.00")
        self.assertContains(resp, "Senior")
        self.assertEqual(self.ada.salary_revisions.count(), 2)                     # opening plus the change
        locked = self.client.get(reverse("salary:detail", args=[self.ada.pk]))
        self.assertContains(locked, "Locked")

    def test_invalid_change_is_not_saved(self):
        resp = self.client.post(reverse("salary:revision_new", args=[self.ada.pk]),
                                {"effective_from": "2026-09-01", "gross_monthly": "0", "reason": "REVIEW"})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(SalaryRevision.objects.exists())

    def test_delete_revision_and_locked_refusal(self):
        keep = services.add_revision(self.ada, date(2026, 12, 1), D("520000"), R.REVIEW)
        self.client.post(reverse("salary:revision_delete", args=[keep.pk]))
        self.assertFalse(SalaryRevision.objects.filter(pk=keep.pk).exists())
        opening = SalaryRevision.objects.get()
        self.run_month(2026, 12)
        resp = self.client.post(reverse("salary:revision_delete", args=[opening.pk]), follow=True)
        self.assertContains(resp, "part of the record")
        self.assertEqual(self.client.get(reverse("salary:revision_delete", args=[opening.pk])).status_code, 405)

    def test_band_warning(self):
        grade = PayGrade.objects.create(company=self.co, name="G3", min_gross=300000, max_gross=450000)
        services.add_revision(self.ada, date(2026, 1, 1), D("500000"), R.REVIEW, grade=grade)
        self.assertContains(self.client.get(reverse("salary:detail", args=[self.ada.pk])), "outside the G3 band")
        self.assertContains(self.client.get(reverse("salary:overview")), "Outside band")

    def test_grades_crud_and_protection(self):
        self.client.post(reverse("salary:grade_new"), {"name": "G1", "min_gross": "100000", "max_gross": "200000"})
        grade = PayGrade.objects.get()
        bad = self.client.post(reverse("salary:grade_new"), {"name": "G2", "min_gross": "300", "max_gross": "200"})
        self.assertContains(bad, "cannot be below the lowest")
        self.client.post(reverse("salary:grade_edit", args=[grade.pk]),
                         {"name": "G1", "min_gross": "100000", "max_gross": "250000"})
        grade.refresh_from_db()
        self.assertEqual(grade.max_gross, D("250000.00"))
        duplicate = self.client.post(reverse("salary:grade_new"),
                                     {"name": "G1", "min_gross": "1", "max_gross": "2"})
        self.assertEqual(duplicate.status_code, 200)
        services.add_revision(self.ada, date(2026, 1, 1), D("200000"), R.REVIEW, grade=grade)
        resp = self.client.post(reverse("salary:grade_delete", args=[grade.pk]), follow=True)
        self.assertContains(resp, "used in salary history")
        self.assertTrue(PayGrade.objects.filter(pk=grade.pk).exists())

    def test_other_companies_are_unreachable(self):
        other = Company.objects.create(name="Beta Ltd")
        stranger = Employee.objects.create(company=other, employee_no="E9", first_name="Z", last_name="Z",
                                           gross_monthly=D("1"))
        rev = SalaryRevision.objects.create(company=other, employee=stranger, effective_from=date(2026, 1, 1),
                                            gross_monthly=D("1"))
        grade = PayGrade.objects.create(company=other, name="X", min_gross=1, max_gross=2)
        staff = get_user_model().objects.create_user("staff")
        CompanyAccess.objects.create(user=staff, company=self.co)
        c = Client()
        c.force_login(staff)
        for url in (reverse("salary:detail", args=[stranger.pk]), reverse("salary:revision_new", args=[stranger.pk]),
                    reverse("salary:grade_edit", args=[grade.pk])):
            self.assertEqual(c.get(url).status_code, 404, url)
        self.assertEqual(c.post(reverse("salary:revision_delete", args=[rev.pk])).status_code, 404)
        self.assertEqual(c.post(reverse("salary:grade_delete", args=[grade.pk])).status_code, 404)
        self.assertNotContains(c.get(reverse("salary:overview")), "E9")
