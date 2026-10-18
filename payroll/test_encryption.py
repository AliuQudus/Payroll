import io
import logging
from types import SimpleNamespace
from unittest import mock

from cryptography.fernet import Fernet
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase, override_settings

from . import encryption, services
from .forms import CompanyForm
from .models import Company

import importlib

PASSWORD = "hunter2-mail-password"


def raw_password(pk):
    with connection.cursor() as cursor:
        cursor.execute("SELECT smtp_password FROM payroll_company WHERE id = %s", [pk])
        return cursor.fetchone()[0]


def set_raw(pk, value):
    with connection.cursor() as cursor:
        cursor.execute("UPDATE payroll_company SET smtp_password = %s WHERE id = %s", [value, pk])


class FieldTests(TestCase):
    def test_it_reads_back_exactly_what_was_saved(self):
        company = Company.objects.create(name="Alpha", smtp_password=PASSWORD)
        self.assertEqual(Company.objects.get(pk=company.pk).smtp_password, PASSWORD)

    def test_the_database_holds_only_ciphertext(self):
        company = Company.objects.create(name="Alpha", smtp_password=PASSWORD)
        raw = raw_password(company.pk)
        self.assertTrue(raw.startswith("enc1:"))
        self.assertNotIn("hunter2", raw)
        self.assertNotIn(PASSWORD, raw)

    def test_the_same_password_encrypts_differently_each_time(self):
        a = Company.objects.create(name="A", smtp_password=PASSWORD)
        b = Company.objects.create(name="B", smtp_password=PASSWORD)
        self.assertNotEqual(raw_password(a.pk), raw_password(b.pk))               # a fresh random value every time

    def test_an_empty_password_stays_empty(self):
        company = Company.objects.create(name="Alpha")
        self.assertEqual(raw_password(company.pk), "")                              # "not set" is visible without the key
        self.assertEqual(Company.objects.get(pk=company.pk).smtp_password, "")

    def test_a_password_that_looks_like_ciphertext_is_still_a_password(self):
        company = Company.objects.create(name="Alpha", smtp_password="enc1:not-really")
        self.assertEqual(Company.objects.get(pk=company.pk).smtp_password, "enc1:not-really")

    def test_unusual_characters_survive(self):
        for password in ("pässwörd ünïcode ✓", "with spaces  and\ttabs", "quote'\"s and \\ slashes", "x" * 500):
            company = Company.objects.create(name="C", smtp_password=password)
            self.assertEqual(Company.objects.get(pk=company.pk).smtp_password, password)

    def test_a_value_saved_before_encryption_is_read_and_then_encrypted_on_save(self):
        company = Company.objects.create(name="Alpha", smtp_password="x")
        set_raw(company.pk, "old-plain-text")                                       # as it was before this change
        loaded = Company.objects.get(pk=company.pk)
        self.assertEqual(loaded.smtp_password, "old-plain-text")
        loaded.save()
        self.assertTrue(raw_password(company.pk).startswith("enc1:"))
        self.assertEqual(Company.objects.get(pk=company.pk).smtp_password, "old-plain-text")

    def test_a_damaged_value_is_not_decrypted_into_nonsense(self):
        company = Company.objects.create(name="Alpha", smtp_password=PASSWORD)
        raw = raw_password(company.pk)
        set_raw(company.pk, raw[:-4] + "AAAA")                                       # tampered with
        with self.assertLogs("payroll", level="ERROR"):
            self.assertEqual(Company.objects.get(pk=company.pk).smtp_password, "")

    def test_the_wrong_key_reads_as_blank_and_logs_instead_of_breaking_the_page(self):
        company = Company.objects.create(name="Alpha", smtp_password=PASSWORD)
        with override_settings(ENCRYPTION_KEYS=[Fernet.generate_key().decode()]):
            with self.assertLogs("payroll", level="ERROR") as logs:
                loaded = Company.objects.get(pk=company.pk)
            self.assertEqual((loaded.name, loaded.smtp_password), ("Alpha", ""))      # the company still loads
            self.assertIn("PAYROLL_ENCRYPTION_KEY", logs.output[0])
        self.assertEqual(Company.objects.get(pk=company.pk).smtp_password, PASSWORD)  # and the right key still works

    def build(self, company):
        """What the mail connection is given, captured because the test runner replaces the real mail backend."""
        with mock.patch("payroll.services.get_connection") as get_connection:
            services._build_connection(Company.objects.get(pk=company.pk), 587)
        return get_connection.call_args.kwargs

    def test_the_mail_connection_gets_the_real_password(self):
        company = Company.objects.create(name="Alpha", email="hr@a.test", smtp_host="mail.example.test",
                                         smtp_password=PASSWORD)
        self.assertEqual(self.build(company)["password"], PASSWORD)                 # decrypted, not "enc1:..."

    def test_gmail_app_passwords_are_still_cleaned_after_decrypting(self):
        company = Company.objects.create(name="G", smtp_host="smtp.gmail.com", smtp_password="abcd efgh ijkl mnop")
        self.assertEqual(self.build(company)["password"], "abcdefghijklmnop")


class CompanyFormTests(TestCase):
    def test_leaving_the_password_blank_keeps_the_stored_one(self):
        company = Company.objects.create(name="Alpha", email="hr@a.test", smtp_password=PASSWORD)
        form = CompanyForm({"name": "Alpha", "email": "hr@a.test", "smtp_host": "smtp.gmail.com", "smtp_port": 587,
                            "smtp_username": "", "smtp_password": "", "smtp_use_tls": "on"}, instance=company)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.assertEqual(Company.objects.get(pk=company.pk).smtp_password, PASSWORD)

    def test_typing_a_new_one_replaces_it_and_it_is_encrypted(self):
        company = Company.objects.create(name="Alpha", email="hr@a.test", smtp_password=PASSWORD)
        form = CompanyForm({"name": "Alpha", "email": "hr@a.test", "smtp_host": "smtp.gmail.com", "smtp_port": 587,
                            "smtp_password": "brand-new-password", "smtp_use_tls": "on"}, instance=company)
        form.save()
        self.assertEqual(Company.objects.get(pk=company.pk).smtp_password, "brand-new-password")
        self.assertNotIn("brand-new", raw_password(company.pk))

    def test_the_page_never_shows_the_password(self):
        from django.contrib.auth import get_user_model
        from django.test import Client
        from django.urls import reverse
        Company.objects.create(name="Alpha", email="hr@a.test", smtp_password=PASSWORD)
        client = Client()
        client.force_login(get_user_model().objects.create_superuser("root", password="x"))
        resp = client.get(reverse("company_edit"))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, PASSWORD)
        self.assertNotContains(resp, "enc1:")

    def test_the_admin_site_cannot_show_or_blank_it(self):
        from django.contrib import admin
        model_admin = admin.site._registry[Company]
        self.assertIn("smtp_password", model_admin.exclude)


class KeyRotationTests(TestCase):
    OLD, NEW = Fernet.generate_key().decode(), Fernet.generate_key().decode()

    def rotate(self):
        out = io.StringIO()
        call_command("rotate_encryption_key", stdout=out)
        return out.getvalue()

    def test_a_key_can_be_replaced_without_losing_anything(self):
        with override_settings(ENCRYPTION_KEYS=[self.OLD]):
            company = Company.objects.create(name="Alpha", smtp_password=PASSWORD)
        with override_settings(ENCRYPTION_KEYS=[self.NEW, self.OLD]):                # new key first, old still allowed
            self.assertEqual(Company.objects.get(pk=company.pk).smtp_password, PASSWORD)
            self.assertIn("Re-encrypted 1 value", self.rotate())
        with override_settings(ENCRYPTION_KEYS=[self.NEW]):                          # the old key is no longer needed
            self.assertEqual(Company.objects.get(pk=company.pk).smtp_password, PASSWORD)
        with override_settings(ENCRYPTION_KEYS=[self.OLD]):                          # and no longer works alone
            with self.assertLogs("payroll", level="ERROR"):
                self.assertEqual(Company.objects.get(pk=company.pk).smtp_password, "")

    def test_nothing_is_lost_if_a_value_cannot_be_read(self):
        with override_settings(ENCRYPTION_KEYS=[self.OLD]):
            company = Company.objects.create(name="Alpha", smtp_password=PASSWORD)
        before = raw_password(company.pk)
        with override_settings(ENCRYPTION_KEYS=[self.NEW]):
            with self.assertRaises(CommandError) as ctx:
                self.rotate()
        self.assertIn("could not be decrypted", str(ctx.exception))
        self.assertEqual(raw_password(company.pk), before)                          # left exactly as it was

    def test_empty_and_old_plain_values_are_left_for_normal_saving(self):
        a = Company.objects.create(name="Empty")
        b = Company.objects.create(name="Plain", smtp_password="x")
        set_raw(b.pk, "legacy-plain")
        self.assertIn("Re-encrypted 0 value", self.rotate())
        self.assertEqual((raw_password(a.pk), raw_password(b.pk)), ("", "legacy-plain"))


class MigrationTests(TestCase):
    """The data step of migration 0011, run directly against rows in the state they would be in."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.migration = importlib.import_module("payroll.migrations.0011_encrypt_smtp_password")

    def run_step(self, function):
        from django.apps import apps
        function(apps, SimpleNamespace(connection=connection))

    def test_plain_text_passwords_are_encrypted_and_nothing_else_is_touched(self):
        plain = Company.objects.create(name="Plain")
        done = Company.objects.create(name="Done", smtp_password="already")
        empty = Company.objects.create(name="Empty")
        set_raw(plain.pk, "stored-before-this-change")
        done_raw = raw_password(done.pk)
        self.run_step(self.migration.encrypt_existing)
        self.assertTrue(raw_password(plain.pk).startswith("enc1:"))
        self.assertNotIn("stored-before", raw_password(plain.pk))
        self.assertEqual(Company.objects.get(pk=plain.pk).smtp_password, "stored-before-this-change")
        self.assertEqual(raw_password(done.pk), done_raw)                          # not encrypted twice
        self.assertEqual(raw_password(empty.pk), "")

    def test_running_it_twice_changes_nothing_more(self):
        company = Company.objects.create(name="Plain")
        set_raw(company.pk, "plain")
        self.run_step(self.migration.encrypt_existing)
        once = raw_password(company.pk)
        self.run_step(self.migration.encrypt_existing)
        self.assertEqual(raw_password(company.pk), once)

    def test_rolling_the_migration_back_restores_readable_text(self):
        company = Company.objects.create(name="Alpha", smtp_password=PASSWORD)
        self.run_step(self.migration.decrypt_existing)
        self.assertEqual(raw_password(company.pk), PASSWORD)                        # plain again, as before this migration


class AuditTests(TestCase):
    def test_the_audit_log_never_records_the_password_or_its_ciphertext(self):
        from django.apps import apps
        if not apps.is_installed("audit"):
            self.skipTest("the audit log is not installed")
        import json
        from audit.models import AuditEntry
        company = Company.objects.create(name="Alpha", smtp_password=PASSWORD)
        company.smtp_password = "changed-password-2"
        company.save()
        blob = json.dumps([e.changes for e in AuditEntry.objects.filter(model_label="payroll.company")])
        for secret in (PASSWORD, "changed-password-2", "enc1:", raw_password(company.pk)[:20]):
            self.assertNotIn(secret, blob)
