from unittest import mock

from django.test import SimpleTestCase

from .mail import PayrollSMTPBackend


class BackendTests(SimpleTestCase):
    def test_connects_the_way_smtplib_does(self):
        with mock.patch("payroll.mail.smtplib.SMTP") as smtp:
            smtp.return_value.esmtp_features = {}
            backend = PayrollSMTPBackend(host="smtp.example.com", port=587, username="u", password="p",
                                         use_tls=True, use_ssl=False, timeout=20)
            self.assertTrue(backend.open())
        smtp.assert_called_once_with("smtp.example.com", 587, timeout=20)     # no custom computer name
        self.assertEqual([c[0] for c in smtp.return_value.method_calls], ["ehlo", "starttls", "ehlo", "login"])

    def test_a_rejected_password_is_reported_as_such(self):
        with mock.patch("payroll.mail.smtplib.SMTP") as smtp:
            smtp.return_value.esmtp_features = {"auth": "LOGIN PLAIN XOAUTH2"}
            PayrollSMTPBackend(host="h", port=587, username="u", password="p", use_tls=True,
                               use_ssl=False, timeout=20).open()
        self.assertEqual(smtp.return_value.esmtp_features["auth"], "PLAIN")

    def test_ssl_port_uses_ssl(self):
        with mock.patch("payroll.mail.smtplib.SMTP_SSL") as smtp_ssl:
            smtp_ssl.return_value.esmtp_features = {}
            backend = PayrollSMTPBackend(host="smtp.example.com", port=465, username="u", password="p",
                                         use_tls=False, use_ssl=True, timeout=20)
            backend.open()
        self.assertEqual([c[0] for c in smtp_ssl.return_value.method_calls], ["login"])
