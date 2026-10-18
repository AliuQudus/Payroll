from datetime import timedelta
from unittest import mock, skipUnless

from django.apps import apps
from django.contrib.auth import authenticate, get_user_model
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import lockout
from .clientip import client_ip
from .forms import LockoutAuthenticationForm
from .models import LoginFailure

GOOD, BAD = "Correct-horse-battery-9", "wrong-guess-123"


class LockoutCase(TestCase):
    def setUp(self):
        users = get_user_model().objects
        self.ada = users.create_user("ada", password=GOOD)
        self.tunde = users.create_user("tunde", password=GOOD)

    def attempt(self, username="ada", password=BAD, **extra):
        return Client(**extra).post(reverse("login"), {"username": username, "password": password})

    def guess_wrong(self, times, username="ada", **extra):
        for _ in range(times):
            self.attempt(username, **extra)

    def signed_in(self, resp):
        return resp.wsgi_request.user.is_authenticated


class LockoutTests(LockoutCase):
    def test_the_login_page_uses_the_lockout_form(self):
        self.assertIsInstance(Client().get(reverse("login")).context["form"], LockoutAuthenticationForm)

    def test_wrong_passwords_are_counted_and_the_sixth_attempt_is_refused_even_if_correct(self):
        self.guess_wrong(5)
        self.assertEqual(LoginFailure.objects.filter(username="ada").count(), 5)
        resp = self.attempt(password=GOOD)
        self.assertContains(resp, "Too many failed sign-in attempts")
        self.assertContains(resp, "Try again in")
        self.assertFalse(self.signed_in(resp))

    def test_below_the_limit_a_correct_password_works_and_clears_the_count(self):
        self.guess_wrong(4)
        resp = self.attempt(password=GOOD)
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(LoginFailure.objects.filter(username="ada").exists())
        self.guess_wrong(4)                                                        # a fresh allowance
        self.assertEqual(self.attempt(password=GOOD).status_code, 302)

    def test_other_users_are_not_affected_by_one_lock(self):
        self.guess_wrong(5)
        self.assertEqual(self.attempt("tunde", GOOD).status_code, 302)

    def test_the_username_is_matched_regardless_of_capitals(self):
        self.guess_wrong(3, "ADA")
        self.guess_wrong(2, "Ada")
        self.assertContains(self.attempt("ada", GOOD), "Too many failed")

    def test_unknown_usernames_lock_in_the_same_way(self):
        self.guess_wrong(5, "nobody")
        self.assertContains(self.attempt("nobody", GOOD), "Too many failed")      # no hint that the user does not exist

    def test_the_lock_ends_after_the_window(self):
        self.guess_wrong(5)
        LoginFailure.objects.update(created_at=timezone.now() - timedelta(minutes=16))
        self.assertEqual(self.attempt(password=GOOD).status_code, 302)

    def test_the_lock_ends_as_the_oldest_failures_age_out(self):
        self.guess_wrong(5)
        oldest = LoginFailure.objects.order_by("created_at").first()
        LoginFailure.objects.filter(pk=oldest.pk).update(created_at=timezone.now() - timedelta(minutes=16))
        self.assertEqual(self.attempt(password=GOOD).status_code, 302)           # only four are still inside the window

    def test_the_message_says_how_long_to_wait(self):
        self.guess_wrong(5)
        LoginFailure.objects.update(created_at=timezone.now() - timedelta(minutes=10))
        self.assertContains(self.attempt(password=GOOD), "Try again in 5 minutes")

    def test_trying_while_locked_does_not_extend_the_lock(self):
        self.guess_wrong(5)
        self.guess_wrong(10)                                                       # keeps guessing while locked
        self.assertEqual(LoginFailure.objects.filter(username="ada").count(), 5)

    def test_the_backend_refuses_every_caller_not_just_the_form(self):
        self.guess_wrong(5)
        self.assertIsNone(authenticate(None, username="ada", password=GOOD))
        self.assertFalse(Client().login(username="ada", password=GOOD))
        self.assertIsNotNone(authenticate(None, username="tunde", password=GOOD))

    def test_the_admin_site_login_is_covered_too(self):
        get_user_model().objects.create_superuser("boss", password=GOOD)
        url = reverse("admin:login")
        for _ in range(5):
            Client().post(url, {"username": "boss", "password": BAD, "next": "/admin/"})
        resp = Client().post(url, {"username": "boss", "password": GOOD, "next": "/admin/"})
        self.assertContains(resp, "Too many failed sign-in attempts")
        self.assertFalse(resp.wsgi_request.user.is_authenticated)

    def test_nothing_secret_is_stored(self):
        self.attempt(password="a-very-distinctive-wrong-password")
        row = LoginFailure.objects.get()
        self.assertEqual((row.username, row.ip_address), ("ada", "127.0.0.1"))
        self.assertNotIn("distinctive", str([getattr(row, f.name) for f in row._meta.fields]))

    def test_old_records_are_tidied_away(self):
        LoginFailure.objects.create(username="old", created_at=timezone.now() - timedelta(days=2))
        self.attempt()
        self.assertFalse(LoginFailure.objects.filter(username="old").exists())

    def test_settings_control_the_limits(self):
        with override_settings(LOGIN_MAX_FAILURES=2):
            self.guess_wrong(2)
            self.assertContains(self.attempt(password=GOOD), "Too many failed")
        LoginFailure.objects.all().delete()
        with override_settings(LOGIN_LOCKOUT_MINUTES=1):
            self.guess_wrong(5)
            LoginFailure.objects.update(created_at=timezone.now() - timedelta(minutes=2))
            self.assertEqual(self.attempt(password=GOOD).status_code, 302)


class AddressLockTests(LockoutCase):
    @override_settings(LOGIN_MAX_FAILURES_PER_IP=3)
    def test_one_address_guessing_many_usernames_is_stopped(self):
        for name in ("a1", "a2", "a3"):
            self.attempt(name)
        refused = self.attempt("tunde", GOOD)                               # a real user, right password, same address
        self.assertContains(refused, "Too many failed")
        elsewhere = self.attempt("tunde", GOOD, REMOTE_ADDR="10.0.0.9")
        self.assertEqual(elsewhere.status_code, 302)                        # a different address is unaffected


class ClientAddressTests(TestCase):
    factory = RequestFactory()

    def request(self, **meta):
        return self.factory.get("/", **meta)

    def test_the_connection_address_is_used_by_default(self):
        self.assertEqual(client_ip(self.request(REMOTE_ADDR="203.0.113.5")), "203.0.113.5")
        self.assertIsNone(client_ip(None))

    def test_a_header_the_visitor_can_set_is_ignored_unless_configured(self):
        spoofed = self.request(REMOTE_ADDR="203.0.113.5", HTTP_X_FORWARDED_FOR="1.2.3.4", HTTP_X_REAL_IP="1.2.3.4")
        self.assertEqual(client_ip(spoofed), "203.0.113.5")

    @override_settings(CLIENT_IP_HEADER="HTTP_X_REAL_IP")
    def test_a_configured_header_is_used(self):
        self.assertEqual(client_ip(self.request(REMOTE_ADDR="10.0.0.1", HTTP_X_REAL_IP="198.51.100.7")), "198.51.100.7")
        self.assertEqual(client_ip(self.request(REMOTE_ADDR="10.0.0.1")), "10.0.0.1")           # header absent
        self.assertEqual(client_ip(self.request(REMOTE_ADDR="10.0.0.1", HTTP_X_REAL_IP="not-an-ip")), "10.0.0.1")

    @override_settings(CLIENT_IP_HEADER="HTTP_X_FORWARDED_FOR")
    def test_a_list_uses_the_entry_your_own_proxy_added(self):
        request = self.request(REMOTE_ADDR="10.0.0.1", HTTP_X_FORWARDED_FOR="6.6.6.6, 198.51.100.7")
        self.assertEqual(client_ip(request), "198.51.100.7")                # the last entry, not one the visitor wrote

    @override_settings(CLIENT_IP_HEADER="HTTP_X_REAL_IP")
    def test_the_lock_follows_the_real_address_behind_a_proxy(self):
        get_user_model().objects.create_user("ada", password=GOOD)
        with override_settings(LOGIN_MAX_FAILURES_PER_IP=2):
            for _ in range(2):
                Client(HTTP_X_REAL_IP="198.51.100.7").post(reverse("login"), {"username": "x", "password": BAD})
            blocked = Client(HTTP_X_REAL_IP="198.51.100.7").post(reverse("login"), {"username": "ada", "password": GOOD})
            self.assertContains(blocked, "Too many failed")
            fine = Client(HTTP_X_REAL_IP="198.51.100.8").post(reverse("login"), {"username": "ada", "password": GOOD})
            self.assertEqual(fine.status_code, 302)                         # same proxy, different visitor


@skipUnless(apps.is_installed("audit"), "the audit log is not installed")
class AuditAddressTests(TestCase):
    @override_settings(CLIENT_IP_HEADER="HTTP_X_REAL_IP")
    def test_the_audit_log_records_the_real_address_too(self):
        from audit.models import AuditEntry
        get_user_model().objects.create_user("ada", password=GOOD)
        Client(HTTP_X_REAL_IP="198.51.100.7").post(reverse("login"), {"username": "ada", "password": GOOD})
        Client(HTTP_X_REAL_IP="198.51.100.9").post(reverse("login"), {"username": "ada", "password": BAD})
        self.assertEqual(AuditEntry.objects.get(action="LOGIN").ip_address, "198.51.100.7")
        self.assertEqual(AuditEntry.objects.get(action="LOGIN_FAILED").ip_address, "198.51.100.9")


class HealthAndHeaderTests(TestCase):
    def test_health_check(self):
        resp = Client().get("/healthz")
        self.assertEqual((resp.status_code, resp.json()), (200, {"status": "ok"}))

    def test_health_check_reports_a_database_failure_without_details(self):
        with mock.patch("security.views.connection.cursor", side_effect=RuntimeError("secret detail")):
            resp = Client().get("/healthz")
        self.assertEqual(resp.status_code, 503)
        self.assertNotIn("secret", resp.content.decode())

    @override_settings(SECURE_SSL_REDIRECT=True)
    def test_the_health_check_is_not_redirected_but_everything_else_is(self):
        client = Client()
        self.assertEqual(client.get("/healthz").status_code, 200)
        redirected = client.get("/accounts/login/")
        self.assertEqual(redirected.status_code, 301)
        self.assertTrue(redirected["Location"].startswith("https://"))

    def test_pages_cannot_be_framed_by_other_sites(self):
        resp = Client().get(reverse("login"))
        self.assertEqual(resp["X-Frame-Options"], "SAMEORIGIN")
        self.assertEqual(resp["X-Content-Type-Options"], "nosniff")
        self.assertEqual(resp["Referrer-Policy"], "same-origin")

    def test_the_calculator_page_that_frames_a_local_file_still_works(self):
        from payroll.models import Company
        company = Company.objects.create(name="Alpha")
        admin = get_user_model().objects.create_superuser("root", password=GOOD)
        client = Client()
        client.force_login(admin)
        resp = client.get(reverse("calculator"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "<iframe")
        self.assertEqual(resp["X-Frame-Options"], "SAMEORIGIN")


class WsgiEntryPointTests(TestCase):
    def test_a_production_server_can_load_the_app_and_get_an_answer(self):
        """gunicorn and similar servers load config.wsgi:application, so it has to exist and work."""
        from wsgiref.util import setup_testing_defaults

        from config.wsgi import application
        environ = {"PATH_INFO": "/healthz", "REQUEST_METHOD": "GET", "HTTP_HOST": "localhost"}
        setup_testing_defaults(environ)
        seen = {}
        body = b"".join(application(environ, lambda status, headers, exc=None: seen.update(status=status)))
        self.assertTrue(seen["status"].startswith("200"))
        self.assertIn(b"ok", body)
