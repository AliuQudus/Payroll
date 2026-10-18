import csv
import io
from datetime import date
from decimal import Decimal as D

from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client, TestCase
from django.urls import reverse

from hr.models import EmployeeProfile
from payroll import services as payroll_services
from payroll.models import Company, CompanyAccess, Employee, Payslip, PayslipLine, PayrollRun

from . import services
from .models import Authority, RemittancePayment, RemittanceSetting

TODAY = date(2026, 10, 9)


def make_run(company, year, month, status="APPROVED"):
    return PayrollRun.objects.create(company=company, year=year, month=month, status=status)


def add_slip(run, employee, paye, pension_ee=0, pension_er=0, gross=D("500000")):
    slip = Payslip.objects.create(run=run, employee=employee, gross=gross, total_earnings=gross,
                                  total_deductions=D(paye) + D(pension_ee), paye=paye, net_pay=gross,
                                  employer_cost=gross)
    if pension_ee:
        PayslipLine.objects.create(payslip=slip, code="PENSION_EE", name="Pension (employee)",
                                   kind="DEDUCTION", amount=pension_ee)
    if pension_er:
        PayslipLine.objects.create(payslip=slip, code="PENSION_ER", name="Pension (employer)",
                                   kind="EMPLOYER", amount=pension_er)
    return slip


class RemitTestCase(TestCase):
    def setUp(self):
        self.co = Company.objects.create(name="Alpha Ltd")
        self.other = Company.objects.create(name="Beta Ltd")
        mk = lambda co, no, first, last: Employee.objects.create(
            company=co, employee_no=no, first_name=first, last_name=last, gross_monthly=D("500000"))
        self.ada, self.tunde, self.chi = mk(self.co, "E001", "Ada", "Obi"), mk(self.co, "E002", "Tunde", "Bello"), \
            mk(self.co, "E003", "Chi", "Eze")
        self.stranger = mk(self.other, "E001", "Zed", "Zulu")
        users = get_user_model().objects
        self.admin = users.create_superuser("admin", password="x")
        self.staff = users.create_user("staff")
        CompanyAccess.objects.create(user=self.staff, company=self.co)
        self.client = Client()
        self.client.force_login(self.admin)
        # September 2026: PAYE 30,000 + 20,000 + 0; pension 40,000 + 50,000 employee and employer
        self.run = make_run(self.co, 2026, 9)
        add_slip(self.run, self.ada, "30000.00", "24000.00", "30000.00")
        add_slip(self.run, self.tunde, "20000.00", "16000.00", "20000.00")
        add_slip(self.run, self.chi, "0.00")                                   # no pension, no tax
        EmployeeProfile.objects.create(employee=self.ada, job_title="A", hire_date=date(2020, 1, 1),
                                       tax_id="TIN-ADA", rsa_pin="PEN100", pension_administrator="Stanbic IBTC")
        EmployeeProfile.objects.create(employee=self.tunde, job_title="B", hire_date=date(2020, 1, 1),
                                       rsa_pin="PEN200", pension_administrator="Stanbic IBTC")

    def url(self, name, *args):
        return reverse(f"remittances:{name}", args=args)


class ServiceTests(RemitTestCase):
    def test_amounts_due_come_from_payslips(self):
        due = services.amounts_due([self.run])
        self.assertEqual(due[(self.run.pk, Authority.PAYE)], D("50000.00"))
        self.assertEqual(due[(self.run.pk, Authority.PENSION)], D("90000.00"))

    def test_only_approved_and_paid_runs_count(self):
        draft = make_run(self.co, 2026, 10, status="DRAFT")
        add_slip(draft, self.ada, "99999.00")
        make_run(self.other, 2026, 9)
        runs = list(services.runs_in_scope(self.co))
        self.assertEqual(runs, [self.run])

    def test_deadline_rolls_into_next_year(self):
        december = make_run(self.co, 2026, 12)
        self.assertEqual(services.due_date(december, 10), date(2027, 1, 10))
        self.assertEqual(services.due_date(self.run, 10), date(2026, 10, 10))

    def test_status_progression(self):
        deadline = date(2026, 10, 10)
        self.assertEqual(services.status_of(D("100"), D("0"), deadline, TODAY), "Outstanding")
        self.assertEqual(services.status_of(D("100"), D("40"), deadline, TODAY), "Part-paid")
        self.assertEqual(services.status_of(D("100"), D("100"), deadline, TODAY), "Paid")
        self.assertEqual(services.status_of(D("100"), D("40"), deadline, date(2026, 10, 11)), "Overdue")
        self.assertEqual(services.status_of(D("0"), D("0"), deadline, date(2026, 12, 1)), "Nothing due")

    def test_position_reflects_payments_and_company_deadlines(self):
        RemittancePayment.objects.create(company=self.co, run=self.run, authority=Authority.PAYE,
                                         paid_date=date(2026, 10, 5), amount="50000.00")
        RemittancePayment.objects.create(company=self.co, run=self.run, authority=Authority.PENSION,
                                         paid_date=date(2026, 10, 5), amount="30000.00")
        RemittanceSetting.objects.create(company=self.co, authority=Authority.PENSION, due_day=5)
        rows = {r["authority"]: r for r in services.position(self.co, today=TODAY)}
        self.assertEqual(rows[Authority.PAYE]["status"], "Paid")
        self.assertEqual(rows[Authority.PENSION]["outstanding"], D("60000.00"))
        self.assertEqual(rows[Authority.PENSION]["deadline"], date(2026, 10, 5))
        self.assertEqual(rows[Authority.PENSION]["status"], "Overdue")        # 5 Oct has passed on 9 Oct
        summary = services.summary(list(rows.values()))
        self.assertEqual(summary["outstanding"], D("60000.00"))
        self.assertEqual(len(summary["overdue"]), 1)

    def test_paye_schedule_has_identifiers_and_flags_gaps(self):
        data = services.paye_schedule(self.run)
        self.assertEqual(data["total"], D("50000.00"))
        self.assertEqual([r["tin"] for r in data["rows"]], ["TIN-ADA", "", ""])
        self.assertEqual(data["missing_ids"], 2)

    def test_pension_schedule_groups_by_administrator(self):
        EmployeeProfile.objects.filter(employee=self.tunde).update(pension_administrator="")
        data = services.pension_schedule(self.run)
        self.assertEqual([g["pfa"] for g in data["groups"]], ["Stanbic IBTC", "Not set"])   # unknown goes last
        self.assertEqual(data["groups"][0]["total"], D("54000.00"))
        self.assertEqual(data["groups"][1]["total"], D("36000.00"))
        self.assertEqual((data["ee"], data["er"], data["total"]), (D("40000.00"), D("50000.00"), D("90000.00")))
        self.assertEqual(data["missing_ids"], 1)                               # Tunde has a PIN but no administrator
        self.assertNotIn("E003", [r["employee"].employee_no for g in data["groups"] for r in g["rows"]])

    def test_works_without_the_hr_module(self):
        with mock.patch("remittances.integrations.hr_installed", return_value=False):
            paye, pension = services.paye_schedule(self.run), services.pension_schedule(self.run)
            page = self.client.get(self.url("schedule", self.run.pk, "paye"))
        self.assertEqual(paye["total"], D("50000.00"))
        self.assertEqual([g["pfa"] for g in pension["groups"]], ["Not set"])
        self.assertEqual(pension["total"], D("90000.00"))
        self.assertContains(page, "Install the HR module")

    def test_real_payroll_output_matches_the_codes_used(self):
        """Run the actual payroll engine so a rename of the pension or PAYE codes would be caught here."""
        call_command("seed_demo", verbosity=0)
        company = Company.objects.exclude(pk__in=[self.co.pk, self.other.pk]).get()
        run = PayrollRun.objects.create(company=company, year=2026, month=9)
        payroll_services.generate(run)
        run.status = "APPROVED"
        run.save()
        due = services.amounts_due([run])
        self.assertEqual(due[(run.pk, Authority.PAYE)], sum(s.paye for s in run.payslips.all()))
        self.assertGreater(due[(run.pk, Authority.PENSION)], 0)
        self.assertEqual(services.pension_schedule(run)["total"], due[(run.pk, Authority.PENSION)])


class ViewTests(RemitTestCase):
    def test_login_required(self):
        resp = Client().get(self.url("overview"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("login", resp["Location"])

    def test_overview_lists_runs_and_menu(self):
        resp = self.client.get(self.url("overview"))
        self.assertContains(resp, "2026-09")
        self.assertContains(resp, "50,000.00")
        self.assertContains(resp, "90,000.00")
        self.assertContains(resp, ">Remittances</a>")
        self.assertContains(resp, "Deadlines")

    def test_overview_ignores_draft_runs_and_other_companies(self):
        make_run(self.co, 2026, 10, status="DRAFT")
        other_run = make_run(self.other, 2026, 8)
        add_slip(other_run, self.stranger, "77777.00")
        resp = self.client.get(self.url("overview"))
        self.assertNotContains(resp, "2026-10")
        self.assertNotContains(resp, "2026-08")
        self.assertNotContains(resp, "77777")

    def test_schedule_pages_render(self):
        for slug in ("paye", "pension"):
            resp = self.client.get(self.url("schedule", self.run.pk, slug))
            self.assertEqual(resp.status_code, 200)
        self.assertContains(self.client.get(self.url("schedule", self.run.pk, "paye")), "TIN-ADA")
        self.assertContains(self.client.get(self.url("schedule", self.run.pk, "pension")), "Stanbic IBTC")

    def test_unknown_authority_and_draft_run_are_404(self):
        draft = make_run(self.co, 2026, 10, status="DRAFT")
        self.assertEqual(self.client.get(self.url("schedule", self.run.pk, "nhf")).status_code, 404)
        self.assertEqual(self.client.get(self.url("schedule", draft.pk, "paye")).status_code, 404)
        self.assertEqual(self.client.get(self.url("payment_new", draft.pk, "paye")).status_code, 404)

    def test_other_company_is_unreachable(self):
        c = Client()
        c.force_login(self.staff)                                              # Alpha only
        foreign = make_run(self.other, 2026, 9)
        payment = RemittancePayment.objects.create(company=self.other, run=foreign, authority="PAYE",
                                                   paid_date=date(2026, 10, 1), amount="1.00")
        for name, args in [("schedule", (foreign.pk, "paye")), ("csv", (foreign.pk, "pension")),
                           ("payment_new", (foreign.pk, "paye"))]:
            self.assertEqual(c.get(self.url(name, *args)).status_code, 404, name)
        self.assertEqual(c.post(self.url("payment_delete", payment.pk)).status_code, 404)
        self.assertTrue(RemittancePayment.objects.filter(pk=payment.pk).exists())

    def test_csv_exports(self):
        paye = self.client.get(self.url("csv", self.run.pk, "paye"))
        rows = list(csv.reader(io.StringIO(paye.content.decode())))
        self.assertEqual(rows[0], ["Employee no", "Name", "TIN", "Gross pay", "PAYE"])
        self.assertEqual(rows[1][:3], ["E001", "Ada Obi", "TIN-ADA"])
        self.assertEqual(rows[-1], ["", "Total", "", "", "50000.00"])
        pension = list(csv.reader(io.StringIO(self.client.get(self.url("csv", self.run.pk, "pension")).content.decode())))
        self.assertEqual(pension[1], ["Stanbic IBTC", "E001", "Ada Obi", "PEN100", "24000.00", "30000.00", "54000.00"])
        self.assertEqual(pension[-1][-1], "90000.00")

    def test_csv_neutralises_formulas(self):
        self.ada.first_name = "=HYPERLINK(\"http://evil\")"
        self.ada.save()
        body = self.client.get(self.url("csv", self.run.pk, "paye")).content.decode()
        self.assertNotIn(",=HYPERLINK", body)
        self.assertIn("'=HYPERLINK", body)

    def test_record_payments_in_parts_until_cleared(self):
        url = self.url("payment_new", self.run.pk, "pension")
        resp = self.client.post(url, {"paid_date": "2026-10-05", "amount": "54000.00", "payee": "Stanbic IBTC",
                                      "reference": "TRX-1"})
        self.assertRedirects(resp, self.url("schedule", self.run.pk, "pension"))
        payment = RemittancePayment.objects.get()
        self.assertEqual((payment.company, payment.recorded_by, payment.authority),
                         (self.co, self.admin, "PENSION"))
        self.assertContains(self.client.get(self.url("schedule", self.run.pk, "pension")), "36,000.00")
        self.client.post(url, {"paid_date": "2026-10-06", "amount": "36000.00"})
        self.assertEqual(services.outstanding(self.run, Authority.PENSION), 0)
        done = self.client.get(url, follow=True)                               # nothing left to pay
        self.assertContains(done, "nothing left to pay")

    def test_payment_validation(self):
        url = self.url("payment_new", self.run.pk, "paye")
        bad = [{"paid_date": "2026-10-05", "amount": "50000.01"},              # more than owed
               {"paid_date": "2026-10-05", "amount": "0"},
               {"paid_date": "2026-10-05", "amount": "-5"},
               {"paid_date": "2999-01-01", "amount": "100"},                   # future
               {"paid_date": "2026-08-31", "amount": "100"}]                   # before the payroll month
        for data in bad:
            self.assertEqual(self.client.post(url, data).status_code, 200, data)
        self.assertFalse(RemittancePayment.objects.exists())

    def test_delete_payment_reopens_the_balance(self):
        self.client.post(self.url("payment_new", self.run.pk, "paye"), {"paid_date": "2026-10-05", "amount": "50000"})
        payment = RemittancePayment.objects.get()
        self.assertEqual(self.client.get(self.url("payment_delete", payment.pk)).status_code, 405)
        self.client.post(self.url("payment_delete", payment.pk))
        self.assertEqual(services.outstanding(self.run, Authority.PAYE), D("50000.00"))

    def test_deadlines_page_is_administrators_only(self):
        c = Client()
        c.force_login(self.staff)
        resp = c.get(self.url("deadlines"))
        self.assertRedirects(resp, reverse("home"), fetch_redirect_response=False)
        self.client.post(self.url("deadlines"), {"paye_day": "15", "pension_day": "7"})
        self.assertEqual(services.due_days(self.co), {Authority.PAYE: 15, Authority.PENSION: 7})
        self.assertEqual(services.due_days(self.other), services.DEFAULT_DUE_DAY)     # not shared
        for bad in ({"paye_day": "0", "pension_day": "7"}, {"paye_day": "29", "pension_day": "7"}):
            self.assertEqual(self.client.post(self.url("deadlines"), bad).status_code, 200)
        self.assertEqual(services.due_days(self.co)[Authority.PAYE], 15)
