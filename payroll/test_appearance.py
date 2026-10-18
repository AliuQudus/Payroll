import re
from pathlib import Path
from unittest import skipUnless

from django.apps import apps
from django.contrib.auth import get_user_model
from django.template.loader import render_to_string
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse

from .context import company as company_context
from .models import Company, CompanyAccess, Employee, UserPreference

STYLE = (Path(__file__).parent / "templates" / "payroll" / "_style.html").read_text()
GOOD = "Correct-horse-battery-9"


def theme_of(response):
    return re.search(r'<html lang="en" data-theme="([^"]*)"', response.content.decode()).group(1)


class AppearanceCase(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Alpha Ltd")
        users = get_user_model().objects
        self.admin = users.create_superuser("boss", password=GOOD)
        self.staff = users.create_user("staff", password=GOOD)
        CompanyAccess.objects.create(user=self.staff, company=self.company)
        self.client = Client()
        self.client.force_login(self.staff)

    def pref(self, user=None):
        return UserPreference.objects.filter(user=user or self.staff).first()


class SavingTheChoiceTests(AppearanceCase):
    def test_nobody_has_chosen_yet_so_the_site_follows_their_device_as_before(self):
        self.assertIsNone(self.pref())
        self.assertEqual(theme_of(self.client.get("/s/employees/")), "system")

    def test_choosing_is_saved_and_applied(self):
        resp = self.client.post(reverse("set_theme"), {"theme": "dark", "next": "/s/employees/"})
        self.assertRedirects(resp, "/s/employees/", fetch_redirect_response=False)
        self.assertEqual(self.pref().theme, "dark")
        self.assertEqual(theme_of(self.client.get("/s/employees/")), "dark")
        self.client.post(reverse("set_theme"), {"theme": "light"})
        self.assertEqual((UserPreference.objects.count(), self.pref().theme), (1, "light"))   # updated, not duplicated
        self.assertEqual(theme_of(self.client.get("/s/employees/")), "light")

    def test_each_person_has_their_own(self):
        self.client.post(reverse("set_theme"), {"theme": "dark"})
        other = Client()
        other.force_login(self.admin)
        self.assertEqual(theme_of(other.get("/s/employees/")), "system")
        other.post(reverse("set_theme"), {"theme": "light"})
        self.assertEqual((self.pref().theme, self.pref(self.admin).theme), ("dark", "light"))

    def test_it_follows_the_person_to_another_device(self):
        self.client.post(reverse("set_theme"), {"theme": "dark"})
        elsewhere = Client()
        elsewhere.post(reverse("login"), {"username": "staff", "password": GOOD})
        self.assertEqual(theme_of(elsewhere.get("/s/employees/")), "dark")

    def test_the_background_save_made_by_the_page_script(self):
        resp = self.client.post(reverse("set_theme"), {"theme": "dark"}, headers={"X-Requested-With": "fetch"})
        self.assertEqual((resp.status_code, resp.content), (204, b""))
        self.assertEqual(self.pref().theme, "dark")
        bad = self.client.post(reverse("set_theme"), {"theme": "neon"}, headers={"X-Requested-With": "fetch"})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(self.pref().theme, "dark")

    def test_only_the_three_real_choices_are_accepted(self):
        for bad in ("", "neon", "DARK", "dark; drop table", None):
            data = {} if bad is None else {"theme": bad}
            resp = self.client.post(reverse("set_theme"), data, follow=True)
            self.assertContains(resp, "not a choice of appearance")
        self.assertIsNone(self.pref())

    def test_it_must_be_a_post_by_someone_signed_in(self):
        self.assertEqual(self.client.get(reverse("set_theme")).status_code, 405)
        anon = Client().post(reverse("set_theme"), {"theme": "dark"})
        self.assertIn("/accounts/login/", anon["Location"])
        self.assertFalse(UserPreference.objects.exists())

    def test_it_is_protected_against_forged_requests(self):
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.staff)
        self.assertEqual(strict.post(reverse("set_theme"), {"theme": "dark"}).status_code, 403)
        self.assertIsNone(self.pref())

    def test_it_cannot_be_used_to_send_people_to_another_site(self):
        for target in ("https://evil.example/x", "//evil.example/", "javascript:alert(1)"):
            resp = self.client.post(reverse("set_theme"), {"theme": "dark", "next": target})
            self.assertEqual(resp["Location"], "/", target)

    def test_a_deleted_person_takes_their_preference_with_them(self):
        self.client.post(reverse("set_theme"), {"theme": "dark"})
        self.staff.delete()
        self.assertFalse(UserPreference.objects.exists())


class ButtonTests(AppearanceCase):
    def button(self, theme):
        if theme:
            self.client.post(reverse("set_theme"), {"theme": theme})
        return self.client.get("/s/employees/").content.decode()

    def test_the_button_offers_the_other_choice(self):
        for current, offers, label in (("dark", "light", "Light mode"), ("light", "dark", "Dark mode"),
                                       ("system", "light", "Light mode")):
            page = self.button(current)
            self.assertIn(f'name="theme" value="{offers}"', page, current)
            self.assertIn(label, page, current)
            self.assertIn(f'aria-label="Switch to {offers} mode"', page, current)

    def test_without_the_script_the_button_is_an_ordinary_form_that_comes_back_here(self):
        """Nothing depends on JavaScript: the fallback is a normal POST that saves and returns to the same page."""
        page = self.client.get("/s/employees/").content.decode()
        form = re.search(r'<form method="post" action="/account/theme/"[^>]*>(.*?)</form>', page, re.S).group(1)
        self.assertIn("csrfmiddlewaretoken", form)
        self.assertIn('name="next" value="/s/employees/"', form)
        resp = self.client.post(reverse("set_theme"), {"theme": "light", "next": "/s/employees/"})
        self.assertEqual((resp.status_code, resp["Location"]), (302, "/s/employees/"))

    def test_the_page_script_saves_in_the_background_and_is_for_signed_in_pages_only(self):
        page = self.client.get("/s/employees/").content.decode()
        self.assertIn("'X-Requested-With':'fetch'", page)
        self.assertIn("X-CSRFToken", page)
        self.assertIn("(prefers-color-scheme: dark)", page)

    def test_the_context_copes_with_signed_out_visitors(self):
        request = RequestFactory().get("/")
        from django.contrib.auth.models import AnonymousUser
        request.user = AnonymousUser()
        self.assertEqual(company_context(request), {"theme": "system", "theme_next": "light"})


class AccountPageTests(AppearanceCase):
    url = reverse("account")

    def test_it_shows_the_three_choices_with_the_current_one_ticked(self):
        page = self.client.get(self.url)
        self.assertEqual([v for v, _ in page.context["themes"]], ["light", "dark", "system"])
        self.assertContains(page, 'value="system" checked')
        self.client.post(reverse("set_theme"), {"theme": "dark"})
        again = self.client.get(self.url).content.decode()
        self.assertIn('value="dark" checked', again)
        self.assertNotIn('value="system" checked', again)

    def test_saving_from_the_page(self):
        resp = self.client.post(self.url, {"theme": "dark"}, follow=True)
        self.assertContains(resp, "appearance is saved")
        self.assertEqual(self.pref().theme, "dark")
        self.assertContains(self.client.post(self.url, {"theme": "neon"}), "Choose one of the options")
        self.assertEqual(self.pref().theme, "dark")

    def test_it_needs_a_sign_in(self):
        self.assertIn("/accounts/login/", Client().get(self.url)["Location"])

    def test_staff_get_the_payroll_layout_and_employees_the_portal_one(self):
        self.assertIn("payroll/base.html", [t.name for t in self.client.get(self.url).templates])
        employee = Employee.objects.create(company=self.company, employee_no="E1", first_name="Ada",
                                           last_name="Obi", gross_monthly=1)
        user = get_user_model().objects.create_user("ada@a.test", password=GOOD)
        from portal.models import EmployeeAccount
        EmployeeAccount.objects.create(user=user, employee=employee)
        portal = Client()
        portal.force_login(user)
        names = [t.name for t in portal.get(self.url).templates]
        self.assertIn("portal/base.html", names)
        self.assertNotIn("payroll/base.html", names)

    def test_the_username_in_the_top_bar_leads_here(self):
        self.assertContains(self.client.get("/s/employees/"), f'<a href="{self.url}"')

    @skipUnless(apps.is_installed("security"), "the security module is not installed")
    def test_two_factor_is_offered_from_here(self):
        page = self.client.get(self.url)
        self.assertContains(page, "Two-factor sign-in")
        self.assertContains(page, "Set up two-factor sign-in")
        self.assertNotContains(page, "Everyone's two-factor status")                  # administrators only
        admin = Client()
        admin.force_login(self.admin)
        self.assertContains(admin.get(self.url), "Everyone's two-factor status")

    @skipUnless(apps.is_installed("security"), "the security module is not installed")
    def test_the_card_reflects_whether_it_is_on_and_whether_it_is_compulsory(self):
        from security import twofactor, totp
        from security.models import TwoFactor
        self.assertContains(self.client.get(self.url), '<span class="tag">Off</span>')
        TwoFactor.objects.create(user=self.staff, secret=totp.new_secret(), confirmed=True)
        self.client.session["2fa_ok"] = self.staff.pk                                  # already verified this session
        session = self.client.session
        session["2fa_ok"] = self.staff.pk
        session.save()
        page = self.client.get(self.url)
        self.assertContains(page, '<span class="tag">On</span>')
        self.assertContains(page, "Manage two-factor sign-in")
        with override_settings(TWO_FACTOR_REQUIRED_FOR_ADMINS=True):
            self.assertTrue(twofactor.policy_requires(self.admin))


class LayoutTests(AppearanceCase):
    def page(self, path="/s/employees/", client=None):
        return (client or self.client).get(path).content.decode()

    def test_the_menu_is_a_vertical_sidebar_not_tabs_across_the_top(self):
        page = self.page()
        self.assertIn('<aside class="side noprint" id="side"', page)
        self.assertIn('<nav class="sidenav" aria-label="Sections">', page)
        self.assertNotIn("<nav class=\"sub\"", page)
        self.assertNotRegex(page, r"<header[^>]*>\s*<div class=\"top\"")             # the old tab bar is gone
        header = re.search(r'<header class="topbar noprint">(.*?)</header>', page, re.S).group(1)
        self.assertNotIn("<nav", header)

    def test_every_module_is_listed_in_order_and_the_current_one_is_marked(self):
        resp = self.client.get("/s/employees/")
        side = re.search(r'<nav class="sidenav".*?</nav>', resp.content.decode(), re.S).group(0)
        shown = re.findall(r'class="item[^"]*"[^>]*>([^<]+)</a>', side)
        self.assertEqual(shown, [m["label"] for m in resp.context["nav_modules"]])         # the same list, in the same order
        self.assertGreater(len(shown), 5)
        self.assertIn('class="item on" aria-current="page">Employees</a>', side)
        self.assertEqual(side.count('class="item on"'), 1)

    def test_the_pages_of_the_current_section_are_nested_beneath_it_only(self):
        admin = Client()
        admin.force_login(self.admin)
        side = re.search(r'<nav class="sidenav".*?</nav>', self.page(reverse("user_list"), admin), re.S).group(0)
        self.assertIn('class="item on" aria-current="page">Setup</a>', side)
        sub = re.search(r'<div class="subnav">(.*?)</div>', side, re.S).group(1)
        self.assertEqual(re.findall(r">([^<]+)</a>", sub),
                         ["Salary elements", "PAYE bands", "Statutory rules", "Company settings", "Users", "Approvers"])
        self.assertIn('class="on" aria-current="page">Users</a>', sub)
        self.assertEqual(side.count('class="subnav"'), 1)                               # no other section is expanded
        self.assertGreater(side.index("Setup"), side.index("Employees"))
        self.assertLess(side.index('class="subnav"'), side.index("Calculator"))          # it opens right under Setup

    def test_on_a_phone_the_menu_slides_out_behind_a_button(self):
        page = self.page()
        self.assertIn('<input type="checkbox" id="navtoggle"', page)
        self.assertIn('<label for="navtoggle" class="menu-btn">', page)
        self.assertIn('<label for="navtoggle" class="scrim', page)
        self.assertIn("@media (max-width:860px)", page)
        self.assertIn(".navtoggle:checked + .app .side{transform:none}", page)

    def test_the_company_controls_and_log_out_are_still_there(self):
        page = self.page()
        for expected in ('action="/company/switch/"', 'action="/accounts/logout/"', "New company", "Log out"):
            self.assertIn(expected, page)

    def test_paper_is_always_light_and_has_no_menus(self):
        for needle in ("@media print", ".side,.topbar", "display:none!important", "--bg:#fff"):
            self.assertIn(needle, STYLE)
        page = self.page()
        self.assertIn('class="side noprint"', page)
        self.assertIn('class="theme-form noprint"', page)

    def test_hidden_fields_are_never_drawn(self):
        self.assertIn("input[type=hidden]{display:none!important}", STYLE)
        self.assertIn("accent-color:var(--ac)", STYLE)                                    # radios are not styled as text boxes

    def test_the_portal_uses_the_same_theme(self):
        employee = Employee.objects.create(company=self.company, employee_no="E1", first_name="Ada",
                                           last_name="Obi", gross_monthly=1)
        user = get_user_model().objects.create_user("ada@a.test", password=GOOD)
        from portal.models import EmployeeAccount
        EmployeeAccount.objects.create(user=user, employee=employee)
        portal = Client()
        portal.force_login(user)
        portal.post(reverse("set_theme"), {"theme": "dark"})
        page = portal.get("/portal/")
        self.assertEqual(theme_of(page), "dark")
        self.assertContains(page, "Light mode")
        self.assertContains(page, 'href="/account/"')
        self.assertContains(page, "<nav>")                                               # the portal keeps its top tabs


def luminance(hex_colour):
    h = hex_colour.lstrip("#")
    h = "".join(c * 2 for c in h) if len(h) == 3 else h
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    lin = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def contrast(a, b):
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def variables(block):
    return dict(re.findall(r"--([a-z]+):(#[0-9a-fA-F]{3,6})", block))


class StylesheetTests(TestCase):
    """The colour tables are tested directly, because a pair that cannot be read is a bug nobody sees in a test run."""

    def setUp(self):
        self.light = variables(re.search(r":root\{color-scheme:light;(.*?)\}", STYLE).group(1))
        self.dark = variables(re.search(r':root\[data-theme="dark"\]\{(.*?)\}', STYLE).group(1))
        self.dark_system = variables(re.search(
            r'@media \(prefers-color-scheme:dark\)\{:root:not\(\[data-theme="light"\]\)\{(.*?)\}\}', STYLE).group(1))
        self.print = variables(re.search(r"@media print\{\s*:root,[^{]*\{(.*?)\}", STYLE, re.S).group(1))

    def test_matching_the_device_gives_exactly_the_same_dark_as_choosing_dark(self):
        self.assertEqual(self.dark, self.dark_system)
        self.assertEqual(set(self.light), set(self.dark))                               # no colour defined for one only

    def test_every_pairing_meets_the_wcag_aa_contrast_standard_in_both_themes(self):
        pairs = [("tx", "bg"), ("tx", "card"), ("mut", "bg"), ("mut", "card"), ("ac", "bg"), ("ac", "card"),
                 ("ac", "acbg"), ("tx", "acbg"), ("er", "card"), ("er", "bg"), ("ok", "card"), ("warn", "card"),
                 ("warn", "bg"), ("bg", "ac")]                                           # the last: text on a primary button
        for name, theme in (("light", self.light), ("dark", self.dark), ("paper", self.print)):
            for foreground, background in pairs:
                if foreground not in theme or background not in theme:
                    continue
                self.assertGreaterEqual(contrast(theme[foreground], theme[background]), 4.5,
                                        f"{name}: {foreground} on {background}")

    def test_the_two_themes_really_are_light_and_dark(self):
        self.assertGreater(luminance(self.light["bg"]), 0.8)
        self.assertLess(luminance(self.dark["bg"]), 0.05)
        self.assertGreater(luminance(self.dark["tx"]), 0.6)
        self.assertLess(luminance(self.light["tx"]), 0.05)
        self.assertEqual(self.print["bg"].lower(), "#fff")

    def test_the_known_ratios_are_computed_correctly(self):
        self.assertAlmostEqual(contrast("#000000", "#ffffff"), 21.0, places=1)         # the calculation itself
        self.assertAlmostEqual(contrast("#777777", "#ffffff"), 4.48, places=1)

    def test_the_stylesheet_is_shared_not_copied(self):
        staff = Path(__file__).parent / "templates" / "payroll" / "base.html"
        portal = Path(__file__).parent.parent / "portal" / "templates" / "portal" / "base.html"
        for template in (staff, portal):
            self.assertIn('{% include "payroll/_style.html" %}', template.read_text())
            self.assertNotIn("--bg:", template.read_text())                              # no private copy of the colours
