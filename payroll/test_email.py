import importlib
from datetime import date
from decimal import Decimal as D

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from . import services
from .models import Company, Employee, PayrollRun, StatutoryRule, TaxBand


class EmailTests(TestCase):
    def setUp(self):
        call_command("seed_demo", verbosity=0)
        self.co = Company.objects.get()
        Company.objects.filter(pk=self.co.pk).update(email="payroll@example.com", smtp_password="secret")
        self.co.refresh_from_db()
        Employee.objects.filter(employee_no="E001").update(email="ada@example.com")
        Employee.objects.filter(employee_no="E002").update(email="tunde@example.com")
        users = get_user_model().objects
        self.maker = users.create_user("maker", password="x")
        self.checker = users.create_user("checker", password="x")
        self.run = PayrollRun.objects.create(company=self.co, year=2026, month=10, created_by=self.maker)
        services.generate(self.run)

    def approve(self):
        services.perform(self.run, "submit", self.maker)
        services.perform(self.run, "approve", self.checker)

    def test_sends_to_each_employee_with_an_email_once(self):
        self.approve()
        result = services.send_payslips(self.run)
        self.assertEqual(result["sent"], 2)
        self.assertEqual(result["skipped"], ["Ngozi Eze"])
        self.assertEqual(len(mail.outbox), 2)
        first = mail.outbox[0]
        self.assertEqual(first.from_email, "Demo Company <payroll@example.com>")
        self.assertEqual(first.to, ["ada@example.com"])
        self.assertEqual(first.subject, "Payslip for 2026-10")
        self.assertIn("Net pay: 394,700.00", first.body)
        self.assertIn("394,700.00", first.alternatives[0][0])
        self.assertEqual(services.send_payslips(self.run)["sent"], 0)          # already sent
        self.assertEqual(services.send_payslips(self.run, resend=True)["sent"], 2)

    def test_needs_approval_and_company_email(self):
        with self.assertRaises(ValueError):
            services.send_payslips(self.run)                                    # still a draft
        self.approve()
        Company.objects.filter(pk=self.co.pk).update(email="")
        self.run.company.refresh_from_db()
        with self.assertRaises(ValueError):
            services.send_payslips(self.run)

    def test_company_settings_keep_saved_password_when_blank(self):
        get_user_model().objects.create_superuser("admin", password="x")
        self.client.login(username="admin", password="x")
        r = self.client.post(reverse("company_edit"), {
            "name": "Demo Company", "tax_id": "", "email": "new@example.com", "smtp_host": "smtp.gmail.com",
            "smtp_port": 587, "smtp_username": "", "smtp_password": "", "smtp_use_tls": "on"})
        self.assertRedirects(r, reverse("home"), fetch_redirect_response=False)
        self.co.refresh_from_db()
        self.assertEqual((self.co.email, self.co.smtp_password), ("new@example.com", "secret"))

    def test_rate_must_be_a_real_percentage(self):
        get_user_model().objects.create_superuser("admin", password="x")
        self.client.login(username="admin", password="x")
        r = self.client.post(reverse("section_new", args=["rules"]),
                             {"code": "PENSION_EMPLOYEE", "value": "250", "effective_from": "2027-01-01"})
        self.assertContains(r, "between 0 and 100")

    def test_test_email_goes_to_the_company_address(self):
        services.send_test_email(self.co)
        self.assertEqual(mail.outbox[0].to, ["payroll@example.com"])

    def test_test_email_button(self):
        get_user_model().objects.create_superuser("admin", password="x")
        self.client.login(username="admin", password="x")
        r = self.client.post(reverse("company_test_email"), follow=True)
        self.assertContains(r, "Test email sent")
        self.assertEqual(len(mail.outbox), 1)

    def test_a_dropped_connection_gives_a_helpful_message(self):
        import smtplib
        from unittest import mock

        class Broken:
            def open(self):
                raise smtplib.SMTPServerDisconnected("Connection unexpectedly closed")

        self.approve()
        with mock.patch("payroll.services.get_connection", return_value=Broken()):
            with self.assertRaises(ValueError) as ctx:
                services.send_payslips(self.run)
        self.assertIn("closed the connection", str(ctx.exception))
        self.assertIn("587", str(ctx.exception))

    def test_a_rejected_password_explains_app_passwords(self):
        import smtplib
        from unittest import mock

        class Broken:
            def open(self):
                raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")

        with mock.patch("payroll.services.get_connection", return_value=Broken()):
            with self.assertRaises(ValueError) as ctx:
                services.send_test_email(self.co)
        self.assertIn("App Password", str(ctx.exception))

    def test_an_unexpected_error_is_shown_instead_of_crashing(self):
        from unittest import mock
        self.approve()
        with mock.patch("payroll.services.render_to_string", side_effect=RuntimeError("boom")):
            result = services.send_payslips(self.run)
        self.assertEqual(result["sent"], 0)
        self.assertIn("unexpected error (RuntimeError): boom", result["failed"][0])

    def test_too_many_login_attempts_is_explained(self):
        import smtplib
        from unittest import mock

        class Broken:
            def open(self):
                raise smtplib.SMTPAuthenticationError(454, b"4.7.0 Too many login attempts, please try again later")

        with mock.patch("payroll.services.get_connection", return_value=Broken()):
            with self.assertRaises(ValueError) as ctx:
                services.send_test_email(self.co)
        self.assertIn("too many login attempts", str(ctx.exception))


class PercentMigrationTests(TestCase):
    def test_old_fraction_rates_are_converted(self):
        co = Company.objects.create(name="Old data")
        for lo, hi, rate in [(0, 800000, "0"), (800000, 3000000, "0.15"), (3000000, None, "0.18")]:
            TaxBand.objects.create(company=co, effective_from=date(2026, 1, 1), lower_limit=lo, upper_limit=hi, rate=D(rate))
        StatutoryRule.objects.create(company=co, code="PENSION_EMPLOYEE", value=D("0.08"), effective_from=date(2026, 1, 1))
        StatutoryRule.objects.create(company=co, code="PENSION_EMPLOYER", value=D("10"), effective_from=date(2026, 1, 1))
        StatutoryRule.objects.create(company=co, code="RENT_RELIEF_CAP", value=D("500000"), effective_from=date(2026, 1, 1))
        importlib.import_module("payroll.migrations.0005_percent_rates").to_percent(apps, None)
        self.assertEqual(sorted(b.rate for b in TaxBand.objects.filter(company=co)), [D("0"), D("15"), D("18")])
        values = {r.code: r.value for r in StatutoryRule.objects.filter(company=co)}
        self.assertEqual(values, {"PENSION_EMPLOYEE": D("8"), "PENSION_EMPLOYER": D("10"), "RENT_RELIEF_CAP": D("500000")})


class TimeoutTests(TestCase):
    def company(self, port):
        return Company.objects.create(name="T", email="a@example.com", smtp_host="smtp.gmail.com", smtp_port=port)

    def test_a_timeout_on_every_port_is_explained(self):
        from unittest import mock

        class Slow:
            def open(self):
                raise TimeoutError("timed out")

        with mock.patch("payroll.services.get_connection", return_value=Slow()), \
             mock.patch("payroll.services.time.sleep"):
            with self.assertRaises(ValueError) as ctx:
                services.send_test_email(self.company(587))
        self.assertIn("did not answer in time", str(ctx.exception))

    def test_port_587_blocked_falls_back_to_465(self):
        from unittest import mock
        tried = []

        class Conn:
            def __init__(self, port):
                self.port = port
            def open(self):
                if self.port == 587:
                    raise TimeoutError("blocked")
                return True
            def close(self):
                pass

        def fake(**kw):
            tried.append(kw["port"])
            return Conn(kw["port"])

        with mock.patch("payroll.services.get_connection", side_effect=fake), \
             mock.patch("payroll.services.time.sleep"), \
             mock.patch("payroll.services.EmailMultiAlternatives"):
            services.send_test_email(self.company(587))
        self.assertEqual(tried, [587, 465])

    def test_a_wrong_password_is_not_retried_on_another_port(self):
        from unittest import mock
        import smtplib
        tried = []

        class Bad:
            def open(self):
                raise smtplib.SMTPAuthenticationError(535, b"nope")

        def fake(**kw):
            tried.append(kw["port"])
            return Bad()

        with mock.patch("payroll.services.get_connection", side_effect=fake):
            with self.assertRaises(ValueError) as ctx:
                services.send_test_email(self.company(587))
        self.assertEqual(tried, [587])
        self.assertIn("App Password", str(ctx.exception))
