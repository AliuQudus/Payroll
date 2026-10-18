"""The settings are tested by starting a fresh Python with a chosen environment, because they are read once at start-up."""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase

from cryptography.fernet import Fernet

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET = "k7Qe2mZxR9vLw4NtYpA8dHsU5cJfB3gXoI6aKzMq1TyVrEnCuW0lDbPhSjFi"            # 60 characters, nothing like a default
FERNET_KEY = Fernet.generate_key().decode()
PRODUCTION = {"PAYROLL_ENV": "production", "PAYROLL_SECRET_KEY": SECRET, "PAYROLL_ENCRYPTION_KEY": FERNET_KEY,
              "PAYROLL_ALLOWED_HOSTS": "payroll.example.com, www.example.com"}
SHOW = """
import json, config.settings as s
names = ["DEBUG", "SECRET_KEY", "ALLOWED_HOSTS", "CSRF_TRUSTED_ORIGINS", "SESSION_COOKIE_SECURE", "CSRF_COOKIE_SECURE",
         "SECURE_SSL_REDIRECT", "SECURE_HSTS_SECONDS", "SECURE_HSTS_INCLUDE_SUBDOMAINS", "SECURE_PROXY_SSL_HEADER",
         "X_FRAME_OPTIONS", "SESSION_COOKIE_AGE", "SESSION_SAVE_EVERY_REQUEST", "STATIC_ROOT", "MEDIA_ROOT", "BACKUP_DIR",
         "DATABASES", "CLIENT_IP_HEADER", "LOGIN_MAX_FAILURES", "LOGIN_LOCKOUT_MINUTES", "AUTHENTICATION_BACKENDS",
         "ENCRYPTION_KEYS"]
print(json.dumps({n: getattr(s, n) for n in names}, default=str))
"""


def run(env=None, code=SHOW, args=None):
    clean = {k: v for k, v in os.environ.items() if not k.startswith("PAYROLL_")}
    clean.update(env or {})
    clean["DJANGO_SETTINGS_MODULE"] = "config.settings"
    command = [sys.executable] + (args or ["-c", code])
    return subprocess.run(command, cwd=BASE_DIR, env=clean, capture_output=True, text=True, timeout=120)


def settings_of(env=None):
    result = run(env)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


class DevelopmentTests(TestCase):
    def test_development_needs_no_configuration_and_is_unchanged(self):
        s = settings_of()
        self.assertTrue(s["DEBUG"])
        self.assertEqual((s["ALLOWED_HOSTS"], s["SECRET_KEY"]), (["localhost", "127.0.0.1"], "dev-only-change-me"))
        self.assertEqual(s["DATABASES"]["default"]["NAME"], str(BASE_DIR / "db.sqlite3"))
        self.assertEqual(s["MEDIA_ROOT"], str(BASE_DIR / "media"))
        self.assertFalse(s["SESSION_COOKIE_SECURE"] or s["SECURE_SSL_REDIRECT"])      # plain http still works locally
        self.assertEqual(s["SECURE_HSTS_SECONDS"], 0)

    def test_the_protections_that_cost_nothing_are_on_everywhere(self):
        s = settings_of()
        self.assertEqual(s["X_FRAME_OPTIONS"], "SAMEORIGIN")
        self.assertTrue(s["SESSION_SAVE_EVERY_REQUEST"])
        self.assertEqual((s["LOGIN_MAX_FAILURES"], s["LOGIN_LOCKOUT_MINUTES"]), (5, 15))
        self.assertEqual(s["AUTHENTICATION_BACKENDS"], ["security.backends.LockoutBackend"])
        self.assertIsNone(s["CLIENT_IP_HEADER"])
        self.assertIsNone(s["SECURE_PROXY_SSL_HEADER"])                              # never trusted unless asked for

    def test_development_derives_an_encryption_key_so_nothing_needs_setting(self):
        a, b = settings_of()["ENCRYPTION_KEYS"], settings_of()["ENCRYPTION_KEYS"]
        self.assertEqual((len(a), a), (1, b))                                         # the same every time...
        self.assertNotEqual(a, settings_of({"PAYROLL_SECRET_KEY": "some-other-development-secret"})["ENCRYPTION_KEYS"])
        Fernet(a[0])                                                                  # ...and a valid key

    def test_values_can_be_overridden(self):
        s = settings_of({"PAYROLL_DEBUG": "0", "PAYROLL_LOGIN_MAX_FAILURES": "3", "PAYROLL_SESSION_HOURS": "2",
                         "PAYROLL_CLIENT_IP_HEADER": "HTTP_X_REAL_IP", "PAYROLL_BEHIND_PROXY": "yes"})
        self.assertFalse(s["DEBUG"])
        self.assertEqual((s["LOGIN_MAX_FAILURES"], s["SESSION_COOKIE_AGE"]), (3, 7200))
        self.assertEqual((s["CLIENT_IP_HEADER"], s["SECURE_PROXY_SSL_HEADER"]),
                         ("HTTP_X_REAL_IP", ["HTTP_X_FORWARDED_PROTO", "https"]))


class ProductionRefusesWeakSetupTests(TestCase):
    def refused(self, env, expected):
        result = run(env)
        self.assertNotEqual(result.returncode, 0, "production started with a weak setting")
        self.assertIn("ImproperlyConfigured", result.stderr)
        self.assertIn(expected, result.stderr)

    def test_no_secret_key(self):
        self.refused({k: v for k, v in PRODUCTION.items() if k != "PAYROLL_SECRET_KEY"}, "PAYROLL_SECRET_KEY")

    def test_a_short_or_repetitive_secret_key(self):
        self.refused({**PRODUCTION, "PAYROLL_SECRET_KEY": "short"}, "too short")
        self.refused({**PRODUCTION, "PAYROLL_SECRET_KEY": "a" * 60}, "repetitive")

    def test_the_development_key_is_not_accepted(self):
        self.refused({**PRODUCTION, "PAYROLL_SECRET_KEY": "dev-only-change-me"}, "too short")

    def test_no_allowed_hosts(self):
        self.refused({k: v for k, v in PRODUCTION.items() if k != "PAYROLL_ALLOWED_HOSTS"}, "PAYROLL_ALLOWED_HOSTS")
        self.refused({**PRODUCTION, "PAYROLL_ALLOWED_HOSTS": " , "}, "PAYROLL_ALLOWED_HOSTS")

    def test_debug_left_on(self):
        for value in ("1", "true", "yes", "on"):
            self.refused({**PRODUCTION, "PAYROLL_DEBUG": value}, "PAYROLL_DEBUG")

    def test_no_encryption_key(self):
        self.refused({k: v for k, v in PRODUCTION.items() if k != "PAYROLL_ENCRYPTION_KEY"}, "PAYROLL_ENCRYPTION_KEY")

    def test_a_malformed_encryption_key(self):
        for bad in ("short", "not a real key at all, spaces and all", "a" * 44):
            self.refused({**PRODUCTION, "PAYROLL_ENCRYPTION_KEY": bad}, "Fernet.generate_key")
        self.refused({**PRODUCTION, "PAYROLL_ENCRYPTION_OLD_KEYS": f"{FERNET_KEY},oops"}, "Fernet.generate_key")

    def test_bad_numbers_and_unknown_engines(self):
        self.refused({**PRODUCTION, "PAYROLL_SESSION_HOURS": "soon"}, "whole number")
        self.refused({**PRODUCTION, "PAYROLL_DB_ENGINE": "mongodb"}, "PAYROLL_DB_ENGINE")

    def test_postgresql_needs_its_details(self):
        self.refused({**PRODUCTION, "PAYROLL_DB_ENGINE": "postgresql"}, "PAYROLL_DB_NAME")


class ProductionSettingsTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="deploy-test-")
        os.makedirs(f"{self.tmp}/data")                  # as on a real server: the deployment checks open the database

    def env(self, **extra):
        return {**PRODUCTION, "PAYROLL_CSRF_TRUSTED_ORIGINS": "https://payroll.example.com",
                "PAYROLL_DB_PATH": f"{self.tmp}/data/db.sqlite3", "PAYROLL_MEDIA_ROOT": f"{self.tmp}/data/media",
                "PAYROLL_STATIC_ROOT": f"{self.tmp}/static", "PAYROLL_BACKUP_DIR": f"{self.tmp}/backups",
                "PAYROLL_SESSION_HOURS": "8", **extra}

    def test_transport_and_cookie_protections_switch_on(self):
        s = settings_of(self.env())
        self.assertFalse(s["DEBUG"])
        self.assertEqual(s["ALLOWED_HOSTS"], ["payroll.example.com", "www.example.com"])
        self.assertEqual(s["CSRF_TRUSTED_ORIGINS"], ["https://payroll.example.com"])
        self.assertTrue(all(s[k] for k in ("SESSION_COOKIE_SECURE", "CSRF_COOKIE_SECURE", "SECURE_SSL_REDIRECT")))
        self.assertEqual((s["SECURE_HSTS_SECONDS"], s["SESSION_COOKIE_AGE"]), (3600, 8 * 3600))
        self.assertEqual(s["SECRET_KEY"], SECRET)

    def test_encryption_keys_are_read_in_order(self):
        old = Fernet.generate_key().decode()
        s = settings_of(self.env(PAYROLL_ENCRYPTION_OLD_KEYS=old))
        self.assertEqual(s["ENCRYPTION_KEYS"], [FERNET_KEY, old])                     # newest first: it encrypts

    def test_data_lives_where_it_is_told_to(self):
        s = settings_of(self.env())
        self.assertEqual(s["DATABASES"]["default"]["NAME"], f"{self.tmp}/data/db.sqlite3")
        self.assertEqual((s["MEDIA_ROOT"], s["STATIC_ROOT"], s["BACKUP_DIR"]),
                         (f"{self.tmp}/data/media", f"{self.tmp}/static", f"{self.tmp}/backups"))

    def test_postgresql_settings(self):
        s = settings_of(self.env(PAYROLL_DB_ENGINE="postgresql", PAYROLL_DB_NAME="payroll", PAYROLL_DB_USER="app",
                                 PAYROLL_DB_PASSWORD="pw", PAYROLL_DB_HOST="db.internal"))
        db = s["DATABASES"]["default"]
        self.assertEqual((db["ENGINE"], db["NAME"], db["USER"], db["HOST"], db["CONN_MAX_AGE"]),
                         ("django.db.backends.postgresql", "payroll", "app", "db.internal", 60))

    def test_secure_cookies_and_redirect_can_be_relaxed_deliberately(self):
        s = settings_of(self.env(PAYROLL_SECURE_COOKIES="0", PAYROLL_SSL_REDIRECT="0", PAYROLL_HSTS_SECONDS="0"))
        self.assertFalse(s["SESSION_COOKIE_SECURE"] or s["SECURE_SSL_REDIRECT"] or s["SECURE_HSTS_SECONDS"])

    def deploy_check(self, **extra):
        result = run(self.env(**extra), args=["manage.py", "check", "--deploy"])
        output = result.stdout + result.stderr
        return result.returncode, set(re.findall(r"\((security\.[WE]\d+)\)", output)), output

    def test_djangos_own_deployment_checklist_has_nothing_serious_to_say(self):
        code, ids, output = self.deploy_check()
        self.assertEqual(code, 0, output)
        self.assertFalse([i for i in ids if ".E" in i], output)
        # what remains is a choice for you to make once https is settled: HSTS for subdomains and preloading
        self.assertLessEqual(ids, {"security.W005", "security.W021"}, output)

    def test_with_the_hsts_choices_made_the_checklist_is_clean(self):
        code, ids, output = self.deploy_check(PAYROLL_HSTS_SECONDS="31536000", PAYROLL_HSTS_INCLUDE_SUBDOMAINS="1",
                                              PAYROLL_HSTS_PRELOAD="1")
        self.assertEqual((code, ids), (0, set()), output)
        self.assertIn("no issues", output)

    def test_the_checklist_notices_a_relaxed_setting(self):
        _, ids, _ = self.deploy_check(PAYROLL_SECURE_COOKIES="0", PAYROLL_SSL_REDIRECT="0")
        self.assertTrue({"security.W008", "security.W012", "security.W016"} <= ids, ids)
