from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from .models import PayrollRun


class PageTests(TestCase):
    def setUp(self):
        call_command("seed_demo", verbosity=0)
        get_user_model().objects.create_superuser("admin", password="x")
        self.client.login(username="admin", password="x")

    def test_every_tab_renders(self):
        for name in ("employees", "elements", "bands", "rules", "loans", "absences", "ious", "additions", "deductions"):
            self.assertEqual(self.client.get(reverse("section_list", args=[name])).status_code, 200)
            self.assertEqual(self.client.get(reverse("section_new", args=[name])).status_code, 200)
        for name in ("run_list", "calculator"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200)

    def test_run_flow_through_the_pages(self):
        r = self.client.post(reverse("run_list"), {"year": 2026, "month": 10})
        run = PayrollRun.objects.get()
        self.assertRedirects(r, reverse("run_detail", args=[run.pk]))
        self.client.post(reverse("run_action", args=[run.pk, "generate"]))
        page = self.client.get(reverse("run_detail", args=[run.pk]))
        self.assertContains(page, "394,700.00")
        slip = run.payslips.first()
        self.assertEqual(self.client.get(reverse("payslip", args=[slip.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("run_csv", args=[run.pk])).status_code, 200)

    def test_element_form_rejects_bad_formula(self):
        r = self.client.post(reverse("section_new", args=["elements"]),
                             {"code": "MEAL", "name": "Meal", "kind": "EARNING", "formula": "NOPE*2"})
        self.assertContains(r, "unknown reference")
