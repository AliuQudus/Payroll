import re
import sys
from unittest import mock, skipUnless

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.checks import run_checks
from django.db import connection
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from payroll.models import Company, CompanyAccess, Employee

from . import qr, totp, twofactor
from .models import LoginFailure, RecoveryCode, TwoFactor

GOOD = "Correct-horse-battery-9"
RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"            # the ASCII text 12345678901234567890 in base32


class TotpTests(SimpleTestCase):
    def test_the_official_test_vectors(self):
        """RFC 6238, appendix B, SHA-1: the published eight-digit values, shortened to the last six digits."""
        for moment, expected in ((59, "287082"), (1111111109, "081804"), (1111111111, "050471"),
                                 (1234567890, "005924"), (2000000000, "279037"), (20000000000, "353130")):
            self.assertEqual(totp.code_at(RFC_SECRET, now=moment), expected, moment)

    def test_a_code_is_accepted_in_its_own_30_seconds_and_one_step_either_side(self):
        step = totp.current_step(1111111109)
        code = totp.code_for_step(RFC_SECRET, step)
        for moment in (1111111109, 1111111109 - 30, 1111111109 + 30):
            self.assertEqual(totp.verify(RFC_SECRET, code, now=moment), step, moment)
        for moment in (1111111109 - 90, 1111111109 + 90):                              # a phone clock minutes out
            self.assertIsNone(totp.verify(RFC_SECRET, code, now=moment), moment)

    def test_a_code_works_only_once(self):
        step = totp.current_step(1111111109)
        code = totp.code_for_step(RFC_SECRET, step)
        used = totp.verify(RFC_SECRET, code, last_step=0, now=1111111109)
        self.assertIsNone(totp.verify(RFC_SECRET, code, last_step=used, now=1111111109))
        older = totp.code_for_step(RFC_SECRET, step - 1)
        self.assertIsNone(totp.verify(RFC_SECRET, older, last_step=used, now=1111111109))   # nor an earlier one
        newer = totp.code_for_step(RFC_SECRET, step + 1)
        self.assertEqual(totp.verify(RFC_SECRET, newer, last_step=used, now=1111111109), step + 1)

    def test_malformed_input_is_refused_not_crashed_on(self):
        for bad in (None, "", "12345", "1234567", "abcdef", "12 34", "١٢٣٤٥٦", "0" * 6 + "\n"):
            self.assertIsNone(totp.verify(RFC_SECRET, bad, now=59), repr(bad))
        self.assertEqual(totp.verify(RFC_SECRET, "287 082", now=59), 1)               # spaces are how apps show it

    def test_secrets_and_the_setup_link(self):
        secrets_made = {totp.new_secret() for _ in range(50)}
        self.assertEqual(len(secrets_made), 50)
        self.assertTrue(all(re.fullmatch(r"[A-Z2-7]{32}", s) for s in secrets_made))
        self.assertEqual(totp.code_at(RFC_SECRET.lower(), now=59), "287082")             # case and spacing do not matter
        uri = totp.provisioning_uri("ABC234", "ada@a.test", "Acme Payroll")
        self.assertTrue(uri.startswith("otpauth://totp/Acme%20Payroll%3Aada%40a.test?secret=ABC234&issuer=Acme%20Payroll"))
        self.assertIn("digits=6", uri)
        self.assertIn("period=30", uri)


class FailClosedTests(SimpleTestCase):
    def test_a_missing_or_broken_secret_never_accepts_a_code(self):
        """A blank secret still produces codes anyone could compute, so it must be refused outright."""
        for secret in ("", "   ", None, "not base32 !!!", "1"):
            for code in (totp.code_at("") if secret in ("", "   ", None) else "123456", "000000", "123456"):
                self.assertIsNone(totp.verify(secret, code), repr(secret))


class TfCase(TestCase):
    def setUp(self):
        users = get_user_model().objects
        self.company = Company.objects.create(name="Alpha Ltd")
        self.admin = users.create_superuser("boss", password=GOOD)
        self.staff = users.create_user("staff", password=GOOD)
        CompanyAccess.objects.create(user=self.staff, company=self.company)

    def enable(self, user):
        tf = TwoFactor.objects.create(user=user, secret=totp.new_secret(), confirmed=True, confirmed_at=timezone.now())
        return tf, twofactor._new_codes(user)

    def code(self, user):
        """A currently valid code that has not been used."""
        TwoFactor.objects.filter(user=user).update(last_step=0)
        return totp.code_at(TwoFactor.objects.get(user=user).secret)

    def password_login(self, username="staff", password=GOOD, **extra):
        client = Client(**extra)
        client.post(reverse("login"), {"username": username, "password": password})
        return client

    def verified(self, username="staff"):
        client = self.password_login(username)
        user = get_user_model().objects.get(username=username)
        client.post(reverse("security:verify"), {"code": self.code(user)})
        return client

    def location(self, response):
        return response["Location"]


class ServiceTests(TfCase):
    def test_a_pending_setup_keeps_the_same_secret_and_stores_it_encrypted(self):
        first = twofactor.pending(self.staff)
        self.assertEqual(twofactor.pending(self.staff).secret, first.secret)
        self.assertFalse(first.confirmed)
        self.assertFalse(twofactor.is_enabled(self.staff))
        with connection.cursor() as cursor:
            cursor.execute("SELECT secret FROM security_twofactor WHERE user_id = %s", [self.staff.pk])
            raw = cursor.fetchone()[0]
        self.assertTrue(raw.startswith("enc1:"))
        self.assertNotIn(first.secret, raw)

    def test_confirming_needs_the_right_code_and_returns_recovery_codes_once(self):
        pending = twofactor.pending(self.staff)
        self.assertIsNone(twofactor.confirm(self.staff, "000000"))
        self.assertFalse(twofactor.is_enabled(self.staff))
        codes = twofactor.confirm(self.staff, totp.code_at(pending.secret))
        self.assertTrue(twofactor.is_enabled(self.staff))
        self.assertEqual(len(codes), 8)
        self.assertEqual(len(set(codes)), 8)
        self.assertTrue(all(re.fullmatch(r"[a-z2-9]{5}-[a-z2-9]{5}", c) for c in codes))
        self.assertIsNone(twofactor.pending(self.staff))                                # already on: nothing pending

    def test_the_confirming_code_cannot_be_used_again_to_sign_in(self):
        pending = twofactor.pending(self.staff)
        code = totp.code_at(pending.secret)
        twofactor.confirm(self.staff, code)
        self.assertIsNone(twofactor.check_code(self.staff, code))

    def test_only_hashes_of_recovery_codes_are_stored(self):
        pending = twofactor.pending(self.staff)
        codes = twofactor.confirm(self.staff, totp.code_at(pending.secret))
        stored = " ".join(RecoveryCode.objects.values_list("code_hash", flat=True))
        for code in codes:
            self.assertNotIn(code.replace("-", ""), stored)
            self.assertNotIn(code, stored)
        self.assertTrue(all(len(h) == 64 for h in RecoveryCode.objects.values_list("code_hash", flat=True)))

    def test_checking_codes(self):
        tf, codes = self.enable(self.staff)
        self.assertEqual(twofactor.check_code(self.staff, totp.code_at(tf.secret)), "totp")
        self.assertIsNone(twofactor.check_code(self.staff, totp.code_at(tf.secret)))     # the same code again
        self.assertEqual(twofactor.check_code(self.staff, codes[0]), "recovery")
        self.assertIsNone(twofactor.check_code(self.staff, codes[0]))                    # a recovery code once, ever
        self.assertEqual(twofactor.codes_left(self.staff), 7)
        self.assertEqual(twofactor.check_code(self.staff, codes[1].upper().replace("-", " ")), "recovery")   # forgiving input
        for wrong in ("", "nonsense", "000000", None):
            self.assertIsNone(twofactor.check_code(self.staff, wrong))

    def test_nothing_is_accepted_for_someone_who_has_not_finished_setup(self):
        pending = twofactor.pending(self.staff)
        self.assertIsNone(twofactor.check_code(self.staff, totp.code_at(pending.secret)))

    def test_a_time_step_can_only_be_claimed_once_even_at_the_same_instant(self):
        tf, _ = self.enable(self.staff)
        self.assertTrue(twofactor._use_step(tf, 100))
        self.assertFalse(twofactor._use_step(tf, 100))
        self.assertFalse(twofactor._use_step(tf, 99))
        self.assertTrue(twofactor._use_step(tf, 101))

    def test_new_recovery_codes_cancel_the_old_ones(self):
        _, old = self.enable(self.staff)
        fresh = twofactor.regenerate_codes(self.staff)
        self.assertEqual(twofactor.codes_left(self.staff), 8)
        self.assertIsNone(twofactor.check_code(self.staff, old[0]))
        self.assertEqual(twofactor.check_code(self.staff, fresh[0]), "recovery")
        with self.assertRaises(ValueError):
            twofactor.regenerate_codes(self.admin)                                       # not switched on

    def test_disabling_removes_everything(self):
        self.enable(self.staff)
        twofactor.disable(self.staff)
        self.assertEqual((TwoFactor.objects.count(), RecoveryCode.objects.count()), (0, 0))
        self.assertFalse(twofactor.is_enabled(self.staff))


class LostKeyTests(TfCase):
    """If PAYROLL_ENCRYPTION_KEY is wrong or lost, two-factor secrets cannot be read. That must lock people out of the
    app-code route, never let a predictable code in."""

    def setUp(self):
        super().setUp()
        self.tf, self.codes = self.enable(self.staff)

    def wrong_key(self):
        from cryptography.fernet import Fernet
        return override_settings(ENCRYPTION_KEYS=[Fernet.generate_key().decode()])

    def test_the_code_anyone_could_compute_from_a_blank_secret_is_refused(self):
        guessable = totp.code_at("")                                                   # what a blank secret would "need"
        with self.wrong_key(), self.assertLogs("payroll", level="ERROR"), self.assertLogs("security", level="ERROR"):
            self.assertEqual(TwoFactor.objects.get(user=self.staff).secret, "")      # unreadable, as designed
            self.assertIsNone(twofactor.check_code(self.staff, guessable))

    def test_the_gate_stays_shut_and_says_nothing_helpful_to_an_attacker(self):
        guessable = totp.code_at("")
        with self.wrong_key(), self.assertLogs("payroll", level="ERROR"), self.assertLogs("security", level="ERROR"):
            client = self.password_login()
            resp = client.post(reverse("security:verify"), {"code": guessable})
            self.assertContains(resp, "not right")
            self.assertIn("/accounts/2fa/verify/", self.location(client.get("/s/employees/")))

    def test_recovery_codes_do_not_depend_on_that_key_so_there_is_a_way_back(self):
        with self.wrong_key(), self.assertLogs("payroll", level="ERROR"), self.assertLogs("security", level="ERROR"):
            self.assertEqual(twofactor.check_code(self.staff, self.codes[0]), "recovery")

    def test_an_administrator_can_reset_it_and_the_person_can_start_again(self):
        with self.wrong_key():
            twofactor.disable(self.staff)                                              # what the Reset button does
            self.assertFalse(twofactor.is_enabled(self.staff))
            pending = twofactor.pending(self.staff)
            self.assertTrue(pending.secret)                                            # a fresh, readable secret

    def test_an_unfinished_setup_with_an_unreadable_secret_is_started_afresh(self):
        twofactor.disable(self.staff)
        old = twofactor.pending(self.staff).secret
        with self.wrong_key(), self.assertLogs("payroll", level="ERROR"):
            fresh = twofactor.pending(self.staff)
            self.assertTrue(fresh.secret)
            self.assertNotEqual(fresh.secret, old)
            self.assertIsNone(twofactor.confirm(self.staff, totp.code_at("")))         # the blank-secret code cannot confirm


class SetupFlowTests(TfCase):
    def setUp(self):
        super().setUp()
        self.client = self.password_login()
        self.url = reverse("security:setup")

    def secret(self):
        return TwoFactor.objects.get(user=self.staff).secret

    def test_the_setup_page_shows_a_qr_code_and_a_typed_key_and_is_never_cached(self):
        page = self.client.get(self.url)
        self.assertEqual(page.context["state"], "setup")
        self.assertIn("no-store", page["Cache-Control"])
        self.assertContains(page, "<svg")
        self.assertContains(page, page.context["key"])
        self.assertEqual(page.context["key"].replace(" ", ""), self.secret())
        self.assertFalse(twofactor.is_enabled(self.staff))                              # looking is not enabling

    def test_without_the_qr_package_the_typed_key_is_shown(self):
        with mock.patch.dict(sys.modules, {"segno": None}):
            self.assertIsNone(qr.svg("otpauth://anything"))
            page = self.client.get(self.url)
        self.assertNotContains(page, "<svg")
        self.assertContains(page, "enter this setup key")
        self.assertContains(page, page.context["key"])

    def test_a_wrong_code_does_not_switch_it_on_and_is_counted(self):
        self.client.get(self.url)
        page = self.client.post(self.url, {"code": "000000"})
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "not right")
        self.assertFalse(twofactor.is_enabled(self.staff))
        self.assertEqual(LoginFailure.objects.filter(username="2fa:staff").count(), 1)

    def test_the_right_code_switches_it_on_and_shows_recovery_codes_once(self):
        self.client.get(self.url)
        page = self.client.post(self.url, {"code": totp.code_at(self.secret())})
        self.assertEqual(page.context["state"], "codes")
        self.assertEqual(len(page.context["codes"]), 8)
        self.assertTrue(twofactor.is_enabled(self.staff))
        after = self.client.get(self.url)
        self.assertEqual(after.context["state"], "on")
        self.assertNotIn("codes", after.context)
        for code in page.context["codes"]:
            self.assertNotContains(after, code)                                         # gone for good
        self.assertNotContains(after, self.secret())                                    # and the secret is not shown again

    def test_the_person_who_just_set_it_up_is_not_asked_again_in_this_session(self):
        self.client.get(self.url)
        self.client.post(self.url, {"code": totp.code_at(self.secret())})
        resp = self.client.get("/")
        self.assertNotIn("/accounts/2fa/verify/", self.location(resp))

    def test_other_open_sessions_are_ended_when_it_is_switched_on(self):
        elsewhere = Client()
        elsewhere.force_login(self.staff)                                                # an older session, perhaps stolen
        self.assertEqual(elsewhere.get("/").status_code, 302)
        self.assertIn("/s/employees/", self.location(elsewhere.get("/")))
        self.client.get(self.url)
        self.client.post(self.url, {"code": totp.code_at(self.secret())})
        self.assertIn("/accounts/login/", self.location(elsewhere.get("/s/employees/")))   # signed out
        self.assertNotIn("/accounts/login/", self.location(self.client.get("/")))           # this one carries on

    def test_a_pending_setup_does_not_change_how_signing_in_works(self):
        self.client.get(self.url)
        resp = self.password_login().get("/")
        self.assertNotIn("/accounts/2fa/verify/", self.location(resp))


class SignInGateTests(TfCase):
    def setUp(self):
        super().setUp()
        self.tf, self.codes = self.enable(self.staff)

    def test_a_password_alone_gets_nobody_past_the_code_screen(self):
        client = self.password_login()
        for path in ("/", "/s/employees/", "/admin/", "/calculator/", reverse("run_list")):
            resp = client.get(path)
            self.assertEqual(resp.status_code, 302, path)
            self.assertIn("/accounts/2fa/verify/", self.location(resp), path)
        self.assertIn("next=", self.location(client.get("/s/employees/")))

    def test_the_code_screen_the_health_check_and_signing_out_stay_reachable(self):
        client = self.password_login()
        self.assertEqual(client.get(reverse("security:verify")).status_code, 200)
        self.assertEqual(client.get("/healthz").status_code, 200)
        self.assertEqual(client.post(reverse("logout")).status_code, 302)

    def test_the_right_code_opens_the_door_and_returns_to_where_they_were_going(self):
        client = self.password_login()
        resp = client.post(reverse("security:verify"), {"code": self.code(self.staff), "next": "/s/employees/"})
        self.assertEqual(self.location(resp), "/s/employees/")
        self.assertEqual(client.get("/s/employees/").status_code, 200)

    def test_a_wrong_code_is_refused_and_the_door_stays_shut(self):
        client = self.password_login()
        resp = client.post(reverse("security:verify"), {"code": "000000"})
        self.assertContains(resp, "not right")
        self.assertIn("/accounts/2fa/verify/", self.location(client.get("/s/employees/")))

    def test_the_same_code_cannot_be_used_on_the_next_sign_in(self):
        code = self.code(self.staff)
        self.password_login().post(reverse("security:verify"), {"code": code})
        second = self.password_login()
        resp = second.post(reverse("security:verify"), {"code": code})
        self.assertContains(resp, "already been used")
        self.assertIn("/accounts/2fa/verify/", self.location(second.get("/s/employees/")))

    def test_every_fresh_password_sign_in_asks_again(self):
        client = self.verified()
        self.assertEqual(client.get("/s/employees/").status_code, 200)
        client.post(reverse("login"), {"username": "staff", "password": GOOD})            # signing in again, same session
        self.assertIn("/accounts/2fa/verify/", self.location(client.get("/s/employees/")))

    def test_a_recovery_code_works_once_and_warns(self):
        client = self.password_login()
        resp = client.post(reverse("security:verify"), {"code": self.codes[0]}, follow=True)
        self.assertContains(resp, "recovery code")
        self.assertContains(resp, "7 left")
        again = self.password_login().post(reverse("security:verify"), {"code": self.codes[0]})
        self.assertContains(again, "not right")

    def test_the_admin_site_sign_in_cannot_skip_the_code(self):
        self.tf.user.is_staff = True
        self.tf.user.is_superuser = True
        self.tf.user.save()
        client = Client()
        client.post(reverse("admin:login"), {"username": "staff", "password": GOOD, "next": "/admin/"})
        self.assertIn("/accounts/2fa/verify/", self.location(client.get("/admin/")))

    def test_it_covers_employees_with_only_a_portal_login_and_uses_their_layout(self):
        employee = Employee.objects.create(company=self.company, employee_no="E1", first_name="Ada", last_name="Obi",
                                           gross_monthly=1)
        user = get_user_model().objects.create_user("ada@a.test", password=GOOD)
        from portal.models import EmployeeAccount
        EmployeeAccount.objects.create(user=user, employee=employee)
        self.enable(user)
        client = self.password_login("ada@a.test")
        self.assertIn("/accounts/2fa/verify/", self.location(client.get("/portal/")))
        page = client.get(reverse("security:verify"))
        self.assertIn("portal/base.html", [t.name for t in page.templates])
        self.assertNotIn("payroll/base.html", [t.name for t in page.templates])

    def test_staff_get_the_payroll_layout(self):
        page = self.password_login().get(reverse("security:verify"))
        self.assertIn("payroll/base.html", [t.name for t in page.templates])

    def test_the_next_address_cannot_send_people_to_another_site(self):
        for target in ("https://evil.example/steal", "//evil.example/", "javascript:alert(1)"):
            client = self.password_login()
            resp = client.post(reverse("security:verify"), {"code": self.code(self.staff), "next": target})
            self.assertEqual(self.location(resp), "/", target)

    def test_the_pass_is_tied_to_the_person(self):
        client = self.password_login()
        session = client.session
        session["2fa_ok"] = self.admin.pk                       # a flag left by someone else
        session.save()
        self.assertIn("/accounts/2fa/verify/", self.location(client.get("/s/employees/")))

    def test_people_without_two_factor_are_never_asked(self):
        other = get_user_model().objects.create_user("plain", password=GOOD)
        CompanyAccess.objects.create(user=other, company=self.company)
        client = self.password_login("plain")
        self.assertEqual(client.get("/s/employees/").status_code, 200)
        self.assertEqual(client.session["2fa_ok"], other.pk)

    def test_the_verify_page_needs_a_password_sign_in_first(self):
        resp = Client().get(reverse("security:verify"))
        self.assertIn("/accounts/login/", self.location(resp))

    def test_a_missing_gate_is_flagged_by_the_system_check(self):
        self.assertNotIn("security.E010", [e.id for e in run_checks()])
        without = [m for m in __import__("django.conf").conf.settings.MIDDLEWARE if "TwoFactor" not in m]
        with override_settings(MIDDLEWARE=without):
            self.assertIn("security.E010", [e.id for e in run_checks()])


class CodeLockoutTests(TfCase):
    def setUp(self):
        super().setUp()
        self.enable(self.staff)

    def test_five_wrong_codes_lock_the_code_screen_even_for_the_right_code(self):
        client = self.password_login()
        for _ in range(5):
            client.post(reverse("security:verify"), {"code": "000000"})
        resp = client.post(reverse("security:verify"), {"code": self.code(self.staff)})
        self.assertContains(resp, "Too many wrong codes")
        self.assertIn("/accounts/2fa/verify/", self.location(client.get("/s/employees/")))

    def test_it_does_not_lock_the_password_and_does_not_extend_itself(self):
        client = self.password_login()
        for _ in range(8):
            client.post(reverse("security:verify"), {"code": "000000"})
        self.assertEqual(LoginFailure.objects.filter(username="2fa:staff").count(), 5)
        again = self.password_login()                                                     # the password still works
        self.assertEqual(again.get("/accounts/2fa/verify/").status_code, 200)

    def test_the_lock_ends_with_the_window(self):
        client = self.password_login()
        for _ in range(5):
            client.post(reverse("security:verify"), {"code": "000000"})
        from datetime import timedelta
        LoginFailure.objects.update(created_at=timezone.now() - timedelta(minutes=16))
        resp = client.post(reverse("security:verify"), {"code": self.code(self.staff)})
        self.assertEqual(resp.status_code, 302)

    def test_a_correct_code_clears_the_count(self):
        client = self.password_login()
        for _ in range(3):
            client.post(reverse("security:verify"), {"code": "000000"})
        client.post(reverse("security:verify"), {"code": self.code(self.staff)})
        self.assertFalse(LoginFailure.objects.filter(username="2fa:staff").exists())


class ManageTests(TfCase):
    def setUp(self):
        super().setUp()
        self.tf, self.codes = self.enable(self.staff)
        self.client = self.verified()

    def confirm_with(self, password=GOOD, code=None):
        return {"password": password, "code": code or self.code(self.staff)}

    def test_new_recovery_codes_need_the_password_and_a_current_code(self):
        before = set(RecoveryCode.objects.values_list("code_hash", flat=True))
        refused = self.client.post(reverse("security:regenerate"), self.confirm_with(password="wrong"), follow=True)
        self.assertContains(refused, "password is not right")
        refused = self.client.post(reverse("security:regenerate"), self.confirm_with(code="000000"), follow=True)
        self.assertContains(refused, "code is not right")
        self.assertEqual(set(RecoveryCode.objects.values_list("code_hash", flat=True)), before)     # nothing changed
        resp = self.client.post(reverse("security:regenerate"), self.confirm_with())
        self.assertEqual(len(resp.context["codes"]), 8)
        self.assertIsNone(twofactor.check_code(self.staff, self.codes[0]))                         # the old ones are dead
        self.assertEqual(twofactor.check_code(self.staff, resp.context["codes"][0]), "recovery")
        self.assertEqual(self.client.get(reverse("security:regenerate")).status_code, 405)

    def test_switching_off_needs_the_password_and_a_current_code(self):
        self.client.post(reverse("security:disable"), self.confirm_with(password="wrong"))
        self.client.post(reverse("security:disable"), self.confirm_with(code="000000"))
        self.assertTrue(twofactor.is_enabled(self.staff))
        resp = self.client.post(reverse("security:disable"), self.confirm_with(), follow=True)
        self.assertContains(resp, "switched off")
        self.assertFalse(twofactor.is_enabled(self.staff))
        self.assertEqual(RecoveryCode.objects.count(), 0)
        self.assertEqual(self.password_login().get("/s/employees/").status_code, 200)          # sign-in is simple again

    def test_a_recovery_code_is_accepted_as_the_proof_when_the_phone_is_lost(self):
        self.client.post(reverse("security:disable"), self.confirm_with(code=self.codes[0]))
        self.assertFalse(twofactor.is_enabled(self.staff))

    def test_wrong_attempts_here_count_towards_the_lock(self):
        for _ in range(5):
            self.client.post(reverse("security:disable"), self.confirm_with(code="000000"))
        resp = self.client.post(reverse("security:disable"), self.confirm_with(), follow=True)
        self.assertContains(resp, "Too many wrong codes")
        self.assertTrue(twofactor.is_enabled(self.staff))


class AdministratorPolicyTests(TfCase):
    @override_settings(TWO_FACTOR_REQUIRED_FOR_ADMINS=True)
    def test_administrators_are_sent_to_set_it_up_before_anything_else(self):
        client = self.password_login("boss")
        for path in ("/", "/s/employees/", "/admin/"):
            resp = client.get(path)
            self.assertIn("/accounts/2fa/", self.location(resp), path)
            self.assertNotIn("verify", self.location(resp), path)
        page = client.get(reverse("security:setup"), follow=True)
        self.assertContains(page, "must switch this on")
        self.assertEqual(client.post(reverse("logout")).status_code, 302)

    @override_settings(TWO_FACTOR_REQUIRED_FOR_ADMINS=True)
    def test_once_set_up_they_carry_on_and_cannot_switch_it_off(self):
        client = self.password_login("boss")
        client.get(reverse("security:setup"))
        secret = TwoFactor.objects.get(user=self.admin).secret
        client.post(reverse("security:setup"), {"code": totp.code_at(secret)})
        self.assertNotIn("/accounts/2fa/", self.location(client.get("/")))
        tf_code = self.code(self.admin)
        resp = client.post(reverse("security:disable"), {"password": GOOD, "code": tf_code}, follow=True)
        self.assertContains(resp, "compulsory for administrators")
        self.assertTrue(twofactor.is_enabled(self.admin))

    @override_settings(TWO_FACTOR_REQUIRED_FOR_ADMINS=True)
    def test_it_does_not_apply_to_other_users(self):
        self.assertEqual(self.password_login("staff").get("/s/employees/").status_code, 200)

    def test_by_default_it_is_optional_for_everyone(self):
        self.assertEqual(self.password_login("boss").get("/s/employees/").status_code, 200)


class AdministratorPageTests(TfCase):
    def setUp(self):
        super().setUp()
        self.enable(self.staff)
        self.pending_user = get_user_model().objects.create_user("halfway", password=GOOD)
        twofactor.pending(self.pending_user)
        self.client = self.password_login("boss")
        self.url = reverse("security:users")

    def test_it_lists_everyone_with_their_state(self):
        rows = {r["user"].username: r for r in self.client.get(self.url).context["rows"]}
        self.assertEqual((rows["staff"]["state"], rows["staff"]["user"].codes), ("on", 8))
        self.assertEqual(rows["halfway"]["state"], "pending")
        self.assertEqual(rows["boss"]["state"], "off")
        self.assertEqual((rows["boss"]["role"], rows["staff"]["role"]), ("Administrator", "Standard user"))

    def test_only_administrators_may_see_or_use_it(self):
        staff_client = self.verified("staff")
        self.assertEqual(staff_client.get(self.url).status_code, 302)
        self.assertEqual(staff_client.post(reverse("security:reset", args=[self.staff.pk])).status_code, 302)
        self.assertTrue(twofactor.is_enabled(self.staff))
        self.assertEqual(Client().get(self.url).status_code, 302)

    def test_resetting_removes_it_and_signs_them_out_everywhere(self):
        elsewhere = self.verified("staff")
        self.assertEqual(elsewhere.get("/s/employees/").status_code, 200)
        resp = self.client.post(reverse("security:reset", args=[self.staff.pk]), follow=True)
        self.assertContains(resp, "was reset for staff")
        self.assertFalse(TwoFactor.objects.filter(user=self.staff).exists())
        self.assertEqual(RecoveryCode.objects.filter(user=self.staff).count(), 0)
        self.assertIn("/accounts/login/", self.location(elsewhere.get("/s/employees/")))
        self.assertEqual(self.password_login("staff").get("/s/employees/").status_code, 200)

    def test_nobody_resets_their_own_here(self):
        self.client.get(self.url)                                    # an ordinary request first, as in real use
        self.enable(self.admin)
        resp = self.client.post(reverse("security:reset", args=[self.admin.pk]), follow=True)
        self.assertTrue(twofactor.is_enabled(self.admin))
        self.assertEqual(self.client.get(reverse("security:reset", args=[self.staff.pk])).status_code, 405)


@skipUnless(apps.is_installed("audit"), "the audit log is not installed")
class AuditTests(TfCase):
    def test_what_matters_is_recorded_and_no_secret_ever_is(self):
        import json
        from audit.models import AuditEntry
        client = self.password_login()
        client.get(reverse("security:setup"))
        secret = TwoFactor.objects.get(user=self.staff).secret
        client.post(reverse("security:setup"), {"code": "000000"})                       # a wrong code
        client.post(reverse("security:setup"), {"code": totp.code_at(secret)})           # then switched on
        events = AuditEntry.objects.filter(action="EVENT").values_list("object_repr", flat=True)
        self.assertIn("Two-factor code rejected", events)
        self.assertTrue(AuditEntry.objects.filter(model_label="security.twofactor", action="UPDATE",
                                                  username="staff").exists())             # switched on, by whom
        blob = json.dumps([e.changes for e in AuditEntry.objects.all()])
        self.assertNotIn(secret, blob)
        self.assertNotIn("enc1:", blob)
        for row in RecoveryCode.objects.all():
            self.assertNotIn(row.code_hash, blob)
        self.assertEqual(AuditEntry.objects.filter(model_label="security.twofactor", action="UPDATE").count(), 1)

    def test_new_recovery_codes_are_recorded(self):
        from audit.models import AuditEntry
        self.enable(self.staff)
        client = self.verified()
        client.post(reverse("security:regenerate"), {"password": GOOD, "code": self.code(self.staff)})
        self.assertIn("Two-factor recovery codes regenerated",
                      AuditEntry.objects.filter(action="EVENT").values_list("object_repr", flat=True))

    def test_a_recovery_code_sign_in_and_an_administrator_reset_are_recorded(self):
        from audit.models import AuditEntry
        _, codes = self.enable(self.staff)
        self.password_login().post(reverse("security:verify"), {"code": codes[0]})
        self.assertIn("Two-factor recovery code used", AuditEntry.objects.filter(action="EVENT")
                      .values_list("object_repr", flat=True))
        self.assertFalse(AuditEntry.objects.filter(model_label="security.recoverycode").exists())   # events, not hash rows
        admin_client = self.password_login("boss")
        admin_client.post(reverse("security:reset", args=[self.staff.pk]))
        gone = AuditEntry.objects.get(model_label="security.twofactor", action="DELETE")
        self.assertEqual(gone.who, "boss")
        self.assertNotIn(RFC_SECRET, str(gone.changes))


class ResetCommandTests(TfCase):
    def run_command(self, *args):
        import io
        from django.core.management import call_command
        out = io.StringIO()
        call_command("reset_two_factor", *args, stdout=out)
        return out.getvalue()

    def test_it_switches_two_factor_off_and_signs_the_person_out_everywhere(self):
        self.enable(self.staff)
        elsewhere = self.verified()
        self.assertEqual(elsewhere.get("/s/employees/").status_code, 200)
        text = self.run_command("staff")
        self.assertIn("was reset for staff", text)
        self.assertFalse(TwoFactor.objects.filter(user=self.staff).exists())
        self.assertEqual(RecoveryCode.objects.filter(user=self.staff).count(), 0)
        self.assertIn("/accounts/login/", self.location(elsewhere.get("/s/employees/")))
        self.assertEqual(self.password_login().get("/s/employees/").status_code, 200)     # password alone works again

    def test_it_says_when_there_was_nothing_to_reset(self):
        self.assertIn("it was not switched on", self.run_command("staff"))

    def test_an_unknown_user_is_reported(self):
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            self.run_command("nobody")

    def test_it_works_when_the_only_administrator_is_locked_out(self):
        """The case the Reset button cannot help with: the last administrator lost their phone and their codes."""
        self.enable(self.admin)
        with override_settings(TWO_FACTOR_REQUIRED_FOR_ADMINS=True):
            client = self.password_login("boss")
            self.assertIn("/accounts/2fa/verify/", self.location(client.get("/")))      # stuck at the code screen
            self.run_command("boss")
            again = self.password_login("boss")
            self.assertIn("/accounts/2fa/", self.location(again.get("/")))                # now asked to set it up afresh
            self.assertNotIn("verify", self.location(again.get("/")))
