from datetime import date
from decimal import Decimal as D
from unittest import mock, skipUnless

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import Client, TestCase
from django.urls import reverse

from leave.models import LeaveRequest, LeaveType
from payroll import services as payroll_services
from payroll.models import (AdjustmentApplication, Company, CompanyAccess, Employee, EmployeeAdjustment, Payslip,
                            PayrollRun)

from . import services
from .forms import ExitForm
from .models import ClearanceItem, Exit, SettlementLine

R = Exit.Reason


def lines(slip):
    return {l.code: l for l in slip.lines.all()}


class ExitCase(TestCase):
    """A real company from seed_demo, so payroll runs go through the actual engine."""

    def setUp(self):
        call_command("seed_demo", verbosity=0)
        self.co = Company.objects.get()
        self.ada = Employee.objects.get(employee_no="E001")
        self.ada.gross_monthly = D("500000")
        self.ada.save()
        self.tunde = Employee.objects.get(employee_no="E002")
        self.admin = get_user_model().objects.create_superuser("admin", password="x")
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

    def leave(self, last_day, employee=None, **kw):
        return services.start_exit(employee or self.ada, kw.pop("reason", R.RESIGNATION), last_day, **kw)

    def line(self, ex, description="Gratuity", amount="200000", kind="EARNING", taxable=True):
        return SettlementLine.objects.create(exit=ex, kind=kind, description=description, amount=D(amount),
                                             taxable=taxable)


class TermsTests(ExitCase):
    def test_what_payroll_is_told_in_each_month(self):
        self.leave(date(2026, 9, 15))
        run = lambda y, m: PayrollRun(year=y, month=m)
        self.assertIsNone(services.terms_for(self.tunde, run(2026, 9)))                      # not leaving
        self.assertIsNone(services.terms_for(self.ada, run(2026, 8)))                        # before the final month
        final = services.terms_for(self.ada, run(2026, 9))
        self.assertEqual((final.final, final.skip, final.days_worked, final.days_in_month), (True, False, 15, 30))
        self.assertEqual(final.factor, D("0.5"))
        self.assertTrue(services.terms_for(self.ada, run(2026, 10)).skip)                    # after: no payslip

    def test_only_open_exits_count(self):
        ex = self.leave(date(2026, 9, 15))
        services.cancel(ex)
        self.assertIsNone(services.terms_for(self.ada, PayrollRun(year=2026, month=10)))

    def test_start_adds_the_default_checklist(self):
        ex = self.leave(date(2026, 9, 15))
        self.assertEqual([i.name for i in ex.items.all()], services.DEFAULT_CLEARANCE)

    def test_one_open_exit_per_employee(self):
        self.leave(date(2026, 9, 15))
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.leave(date(2026, 10, 15))

    def test_any_date_is_allowed_while_no_payroll_has_been_approved(self):
        self.assertIsNone(services.first_open_month(self.co))
        form = ExitForm({"employee": self.ada.pk, "reason": "RESIGNATION", "last_working_day": "2026-09-15"},
                        instance=Exit(company=self.co), company=self.co)
        self.assertTrue(form.is_valid(), form.errors)                  # September, entered in October, is normal
        self.run_month(2026, 12)
        self.assertEqual(services.first_open_month(self.co), date(2027, 1, 1))     # December rolls into January

    def test_form_rules(self):
        self.run_month(2026, 8)                                                              # August is approved
        make = lambda **kw: ExitForm({"employee": self.ada.pk, "reason": "RESIGNATION", **kw},
                                     instance=Exit(company=self.co), company=self.co)
        self.assertIn("already been approved", str(make(last_working_day="2026-08-20").errors))
        self.assertTrue(make(last_working_day="2026-09-01").is_valid())
        self.assertIn("before the notice date", str(make(last_working_day="2026-09-10", notice_date="2026-09-20").errors))
        self.leave(date(2026, 9, 15))
        offered = set(make(last_working_day="2026-09-01").fields["employee"].queryset)
        self.assertNotIn(self.ada, offered)                                                  # already leaving
        self.assertIn(self.tunde, offered)


class FinalPayTests(ExitCase):
    def test_final_month_is_paid_for_the_days_worked(self):
        self.leave(date(2026, 9, 15))
        august = self.run_month(2026, 8)
        self.assertEqual(self.slip(august).gross, D("500000.00"))                            # unaffected before
        self.assertNotIn("PRORATA", lines(self.slip(august)))
        september = self.run_month(2026, 9)
        slip = self.slip(september)
        self.assertEqual(slip.gross, D("250000.00"))                                         # 15 of 30 days
        self.assertEqual(lines(slip)["PRORATA"].amount, D("250000.00"))
        self.assertIn("15 of 30", lines(slip)["PRORATA"].name)
        self.assertEqual(self.slip(september, self.tunde).gross, self.tunde.gross_monthly)   # others unaffected

    def test_nothing_is_paid_after_the_final_month(self):
        self.leave(date(2026, 9, 15))
        self.run_month(2026, 9)
        october = self.run_month(2026, 10)
        self.assertFalse(october.payslips.filter(employee=self.ada).exists())
        self.assertTrue(october.payslips.filter(employee=self.tunde).exists())

    def test_leaving_on_the_last_day_of_the_month_pays_in_full(self):
        ex = self.leave(date(2026, 9, 30))
        self.line(ex)
        slip = self.slip(self.run_month(2026, 9))
        self.assertEqual(slip.gross, D("500000.00"))
        self.assertNotIn("PRORATA", lines(slip))
        self.assertIn(f"SETTLE{ex.lines.get().pk}", lines(slip))

    def test_february_is_judged_against_its_own_length(self):
        self.leave(date(2027, 2, 28))
        self.assertEqual(self.slip(self.run_month(2027, 2)).gross, D("500000.00"))

    def test_loans_are_recovered_in_full(self):
        loan = EmployeeAdjustment.objects.create(company=self.co, employee=self.ada, kind="LOAN", description="Car",
                                                 amount=D("120000"), months=12, start_year=2026, start_month=9)
        normal = lines(self.slip(self.run_month(2026, 9, final=False)))
        self.assertEqual(normal[f"LOAN{loan.pk}"].amount, D("10000.00"))                     # a normal month
        self.leave(date(2026, 9, 20))
        slip = self.slip(self.run_month(2026, 9, final=False) if False else self.regenerate(2026, 9))
        self.assertEqual(lines(slip)[f"LOAN{loan.pk}"].amount, D("120000.00"))                # everything that is left
        self.assertEqual(AdjustmentApplication.objects.get(payslip=slip).amount, D("120000.00"))

    def regenerate(self, year, month):
        run = PayrollRun.objects.get(company=self.co, year=year, month=month)
        payroll_services.generate(run)
        return run

    def test_taxable_payments_are_taxed_once_and_untaxable_ones_are_not(self):
        ex = self.leave(date(2026, 9, 30))
        gratuity = self.line(ex, amount="2000000")
        taxable = self.slip(self.run_month(2026, 9, final=False))
        gratuity.taxable = False
        gratuity.save()
        exempt = self.slip(self.regenerate(2026, 9))
        base = self.slip(self.run_month(2026, 10, final=False), self.tunde)                  # a comparable normal month
        self.assertGreater(taxable.paye, exempt.paye)
        self.assertEqual(exempt.total_earnings, taxable.total_earnings)                      # same money, different tax
        self.assertEqual(exempt.net_pay - taxable.net_pay, taxable.paye - exempt.paye)

    def test_deductions_reduce_the_pay(self):
        ex = self.leave(date(2026, 9, 30))
        before = self.slip(self.run_month(2026, 9, final=False)).net_pay
        self.line(ex, "Notice not served", "100000", kind="DEDUCTION")
        slip = self.slip(self.regenerate(2026, 9))
        self.assertEqual(before - slip.net_pay, D("100000.00"))
        self.assertEqual(lines(slip)[f"SETTLE{ex.lines.get().pk}"].kind, "DEDUCTION")

    def test_salary_history_records_the_full_gross_not_the_part_month(self):
        from salary.models import SalaryBasis
        self.leave(date(2026, 9, 15))
        slip = self.slip(self.run_month(2026, 9))
        self.assertEqual(SalaryBasis.objects.get(payslip=slip).monthly_gross, D("500000.00"))

    def test_absence_is_worked_out_on_the_full_salary(self):
        EmployeeAdjustment.objects.create(company=self.co, employee=self.ada, kind="ABSENCE", description="Away",
                                          start_year=2026, start_month=9, days=3)
        self.leave(date(2026, 9, 15))
        slip = self.slip(self.run_month(2026, 9))
        self.assertEqual(lines(slip)["ABSENCE"].amount, D("50000.00"))                       # 3 days at 500,000 / 30
        self.assertEqual(slip.gross, D("200000.00"))                                         # 250,000 less 50,000


class PreviewTests(ExitCase):
    def test_preview_matches_the_real_payslip(self):
        ex = self.leave(date(2026, 9, 15))
        self.line(ex, amount="300000")
        self.line(ex, "Notice not served", "50000", kind="DEDUCTION")
        shown = services.preview(ex)
        slip = self.slip(self.run_month(2026, 9, final=False))
        self.assertEqual((shown["gross"], shown["net"], shown["paye"]), (slip.gross, slip.net_pay, slip.paye))
        self.assertEqual(shown["prorata_cut"], D("250000.00"))
        self.assertFalse(shown["owes"])

    def test_preview_leaves_nothing_behind(self):
        ex = self.leave(date(2026, 9, 15))
        runs, slips = PayrollRun.objects.count(), Payslip.objects.count()
        services.preview(ex)
        self.assertEqual((PayrollRun.objects.count(), Payslip.objects.count()), (runs, slips))
        self.run_month(2026, 9, final=False)
        slips = Payslip.objects.count()
        services.preview(ex)                                                                  # with a run already there
        self.assertEqual(Payslip.objects.count(), slips)

    def test_a_negative_net_is_flagged(self):
        ex = self.leave(date(2026, 9, 30))
        self.line(ex, "Advance recovery", "5000000", kind="DEDUCTION")
        self.assertTrue(services.preview(ex)["owes"])

    def test_preview_reports_setup_problems_instead_of_crashing(self):
        from payroll.models import SalaryElement
        SalaryElement.objects.all().delete()
        self.assertIn("salary element", services.preview(self.leave(date(2026, 9, 15)))["error"])


class LeavePayoutTests(ExitCase):
    def setUp(self):
        super().setUp()
        self.annual = LeaveType.objects.create(company=self.co, name="Annual", days_per_year=20, is_paid=True,
                                               earns_allowance=True)
        LeaveType.objects.create(company=self.co, name="Sick", days_per_year=10, is_paid=True)   # not paid out

    def test_unused_annual_leave_is_valued_at_the_daily_rate(self):
        ex = self.leave(date(2026, 12, 31))                                                  # a full year accrued
        ((lt, accrued, taken, days, amount),) = services.leave_payouts(ex)
        self.assertEqual((lt, accrued, taken, days), (self.annual, D("20.0"), D("0"), D("20.0")))
        self.assertEqual(amount, D("333333.33"))                                             # 500,000 / 30 x 20

    def test_entitlement_accrues_through_the_year_and_taken_days_are_deducted(self):
        LeaveRequest.objects.create(company=self.co, employee=self.ada, leave_type=self.annual,
                                    start_date=date(2026, 3, 2), end_date=date(2026, 3, 6), status="APPROVED")
        ex = self.leave(date(2026, 6, 30))                                                   # 181 of 365 days: 9.9 accrued
        ((_, accrued, taken, days, amount),) = services.leave_payouts(ex)
        self.assertEqual((accrued, taken, days), (D("9.9"), D("5.0"), D("4.9")))
        self.assertEqual(amount, D("81666.67"))

    def test_nothing_to_pay_when_the_leave_is_used_up(self):
        LeaveRequest.objects.create(company=self.co, employee=self.ada, leave_type=self.annual,
                                    start_date=date(2026, 3, 2), end_date=date(2026, 3, 27), status="APPROVED")
        self.assertEqual(services.leave_payouts(self.leave(date(2026, 6, 30))), [])

    def test_suggestion_becomes_an_editable_line_once(self):
        ex = self.leave(date(2026, 12, 31))
        (added,) = services.add_suggestions(ex)
        self.assertEqual((added.kind, added.amount, added.taxable), ("EARNING", D("333333.33"), True))
        self.assertIn("Unused Annual", added.description)
        self.assertEqual(services.add_suggestions(ex), [])                                   # not duplicated
        added.amount = D("300000")
        added.save()
        self.assertEqual(self.slip(self.run_month(2026, 12)).total_earnings,
                         D("500000") + D("300000"))

    def test_without_the_leave_module_there_is_nothing_to_suggest(self):
        ex = self.leave(date(2026, 12, 31))
        with mock.patch("exits.services.apps.is_installed", return_value=False):
            self.assertEqual(services.leave_payouts(ex), [])


class CompletionTests(ExitCase):
    def test_cannot_complete_until_payroll_and_clearance_are_done(self):
        ex = self.leave(date(2026, 9, 15))
        with self.assertRaises(ValueError) as ctx:
            services.complete(ex, self.admin)
        self.assertIn("not been approved", str(ctx.exception))
        self.assertIn("6 clearance items", str(ctx.exception))
        self.run_month(2026, 9)
        with self.assertRaises(ValueError) as ctx:
            services.complete(ex, self.admin)
        self.assertNotIn("not been approved", str(ctx.exception))
        self.assertIn("6 clearance items", str(ctx.exception))

    def test_completing_takes_the_employee_off_payroll(self):
        ex = self.leave(date(2026, 9, 15))
        self.run_month(2026, 9)
        ex.items.update(is_done=True)
        services.complete(ex, self.admin)
        ex.refresh_from_db()
        self.ada.refresh_from_db()
        self.assertEqual((ex.status, ex.completed_by, self.ada.is_active), ("COMPLETED", self.admin, False))
        self.assertIsNotNone(ex.completed_at)
        with self.assertRaises(ValueError):
            services.complete(ex, self.admin)                                                # only once
        self.assertFalse(self.run_month(2026, 10).payslips.filter(employee=self.ada).exists())

    def test_cancelling_restores_normal_pay(self):
        ex = self.leave(date(2026, 9, 15))
        services.cancel(ex)
        self.assertEqual(self.slip(self.run_month(2026, 9)).gross, D("500000.00"))
        with self.assertRaises(ValueError):
            services.cancel(ex)

    def test_cannot_cancel_after_the_final_month_is_approved(self):
        ex = self.leave(date(2026, 9, 15))
        self.run_month(2026, 9)
        with self.assertRaises(ValueError):
            services.cancel(ex)
        self.assertEqual(Exit.objects.get().status, "OPEN")

    def test_locking(self):
        ex = self.leave(date(2026, 9, 15))
        self.assertFalse(services.is_locked(ex))
        self.run_month(2026, 9, final=False)
        self.assertFalse(services.is_locked(ex))                                             # a draft changes nothing
        run = PayrollRun.objects.get(year=2026, month=9)
        run.status = "APPROVED"
        run.save()
        self.assertTrue(services.is_locked(ex))


class ViewTests(ExitCase):
    def test_login_required(self):
        resp = Client().get(reverse("exits:overview"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("login", resp["Location"])

    def test_menu_and_overview(self):
        ex = self.leave(date(2026, 9, 15))
        ex.items.filter(pk__in=list(ex.items.values_list("pk", flat=True)[:2])).update(is_done=True)
        page = self.client.get(reverse("exits:overview"))
        self.assertContains(page, ">Exits</a>")
        self.assertContains(page, "2 of 6 done")
        self.assertContains(page, "E001")

    def test_starting_an_exit_through_the_page(self):
        resp = self.client.post(reverse("exits:new"), {"employee": self.ada.pk, "reason": "RESIGNATION",
                                                       "last_working_day": "2026-09-15", "notes": "Moving abroad"})
        ex = Exit.objects.get()
        self.assertRedirects(resp, reverse("exits:detail", args=[ex.pk]))
        self.assertEqual((ex.created_by, ex.items.count()), (self.admin, 6))
        self.assertTrue(self.client.get(reverse("exits:new")).status_code == 200)

    def test_detail_shows_the_preview_and_blockers(self):
        ex = self.leave(date(2026, 9, 15))
        self.line(ex, "Pay in lieu of notice", "100000")
        page = self.client.get(reverse("exits:detail", args=[ex.pk]))
        self.assertContains(page, "Final payslip preview")
        self.assertContains(page, "Pay in lieu of notice")
        self.assertContains(page, "15 of 30 days")
        self.assertContains(page, "has not been approved yet")
        self.assertContains(page, "disabled")                                                # cannot complete yet

    def test_pending_run_warns_that_it_must_be_regenerated(self):
        ex = self.leave(date(2026, 9, 15))
        run = self.run_month(2026, 9, final=False)
        run.status = "PENDING"
        run.save()
        self.assertContains(self.client.get(reverse("exits:detail", args=[ex.pk])), "Reopen it to draft")

    def test_checklist_actions(self):
        ex = self.leave(date(2026, 9, 15))
        item = ex.items.first()
        self.assertEqual(self.client.get(reverse("exits:item_toggle", args=[item.pk])).status_code, 405)
        self.client.post(reverse("exits:item_toggle", args=[item.pk]))
        item.refresh_from_db()
        self.assertEqual((item.is_done, item.done_by), (True, self.admin))
        self.client.post(reverse("exits:item_toggle", args=[item.pk]))
        item.refresh_from_db()
        self.assertEqual((item.is_done, item.done_by), (False, None))
        self.client.post(reverse("exits:item_add", args=[ex.pk]), {"name": "Return parking pass"})
        self.assertEqual(ex.items.count(), 7)
        self.client.post(reverse("exits:item_delete", args=[ex.items.last().pk]))
        self.assertEqual(ex.items.count(), 6)

    def test_lines_through_the_pages_and_the_lock(self):
        ex = self.leave(date(2026, 9, 15))
        self.client.post(reverse("exits:line_new", args=[ex.pk]), {
            "kind": "EARNING", "description": "Gratuity", "amount": "150000", "taxable": "on"})
        line = ex.lines.get()
        self.client.post(reverse("exits:line_edit", args=[line.pk]), {
            "kind": "EARNING", "description": "Gratuity", "amount": "175000"})
        line.refresh_from_db()
        self.assertEqual((line.amount, line.taxable), (D("175000.00"), False))
        bad = self.client.post(reverse("exits:line_new", args=[ex.pk]), {"kind": "EARNING", "description": "X",
                                                                          "amount": "0"})
        self.assertContains(bad, "above zero")
        self.run_month(2026, 9)                                                              # final month approved
        resp = self.client.post(reverse("exits:line_delete", args=[line.pk]), follow=True)
        self.assertContains(resp, "can no longer be changed")
        self.assertTrue(SettlementLine.objects.filter(pk=line.pk).exists())
        self.assertContains(self.client.get(reverse("exits:line_new", args=[ex.pk]), follow=True),
                            "can no longer be changed")
        self.assertContains(self.client.get(reverse("exits:edit", args=[ex.pk]), follow=True),
                            "can no longer be changed")

    def test_complete_and_cancel_through_the_pages(self):
        ex = self.leave(date(2026, 9, 15))
        resp = self.client.post(reverse("exits:complete", args=[ex.pk]), follow=True)
        self.assertContains(resp, "not been approved")
        self.run_month(2026, 9)
        ex.items.update(is_done=True)
        resp = self.client.post(reverse("exits:complete", args=[ex.pk]), follow=True)
        self.assertContains(resp, "no longer on payroll")
        self.ada.refresh_from_db()
        self.assertFalse(self.ada.is_active)
        self.assertEqual(self.client.get(reverse("exits:complete", args=[ex.pk])).status_code, 405)

    def test_suggest_button(self):
        LeaveType.objects.create(company=self.co, name="Annual", days_per_year=20, is_paid=True, earns_allowance=True)
        ex = self.leave(date(2026, 12, 31))
        resp = self.client.post(reverse("exits:suggest", args=[ex.pk]), follow=True)
        self.assertContains(resp, "Added 1 line")
        again = self.client.post(reverse("exits:suggest", args=[ex.pk]), follow=True)
        self.assertContains(again, "Nothing to suggest")

    def test_other_companies_are_unreachable(self):
        other = Company.objects.create(name="Beta Ltd")
        stranger = Employee.objects.create(company=other, employee_no="Z1", first_name="Zed", last_name="Zulu",
                                           gross_monthly=D("1"))
        theirs = services.start_exit(stranger, R.RESIGNATION, date(2026, 9, 15))
        item, line = theirs.items.first(), SettlementLine.objects.create(exit=theirs, description="X", amount=D("1"))
        staff = get_user_model().objects.create_user("staff")
        CompanyAccess.objects.create(user=staff, company=self.co)
        c = Client()
        c.force_login(staff)
        for name, args in [("detail", [theirs.pk]), ("edit", [theirs.pk]), ("line_new", [theirs.pk]),
                           ("line_edit", [line.pk])]:
            self.assertEqual(c.get(reverse(f"exits:{name}", args=args)).status_code, 404, name)
        for name, args in [("complete", [theirs.pk]), ("cancel", [theirs.pk]), ("suggest", [theirs.pk]),
                           ("item_add", [theirs.pk]), ("item_toggle", [item.pk]), ("item_delete", [item.pk]),
                           ("line_delete", [line.pk])]:
            self.assertEqual(c.post(reverse(f"exits:{name}", args=args)).status_code, 404, name)
        self.assertNotContains(c.get(reverse("exits:overview")), "Zed")
        self.assertNotIn(stranger, set(c.get(reverse("exits:new")).context["form"].fields["employee"].queryset))


@skipUnless(apps.is_installed("audit"), "the audit log is not installed")
class AuditTests(ExitCase):
    def test_completing_an_exit_is_traced_to_the_person_who_did_it(self):
        from audit.models import AuditEntry
        ex = self.leave(date(2026, 9, 15))
        self.run_month(2026, 9)
        ex.items.update(is_done=True)
        self.client.post(reverse("exits:complete", args=[ex.pk]))
        deactivated = AuditEntry.objects.get(model_label="payroll.employee", action="UPDATE", employee_id=self.ada.pk,
                                             changes__has_key="is active")
        self.assertEqual((deactivated.who, deactivated.changes["is active"]), ("admin", ["Yes", "No"]))
        self.assertTrue(AuditEntry.objects.filter(model_label="exits.exit", action="UPDATE", username="admin").exists())
        self.assertTrue(AuditEntry.objects.filter(model_label="exits.exit", action="CREATE").exists())
