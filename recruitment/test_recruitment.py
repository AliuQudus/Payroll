import os
import shutil
import tempfile
from datetime import date, timedelta
from decimal import Decimal as D
from unittest import mock, skipUnless

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from payroll import services as payroll_services
from payroll.models import Company, CompanyAccess, Employee, PayrollRun

from . import services
from .models import Applicant, Interview, Offer, Onboarding, OnboardingItem, StageEvent, Vacancy

Stage = Applicant.Stage
MEDIA = tempfile.mkdtemp(prefix="recruitment-test-media-")


def pdf(name="cv.pdf"):
    return SimpleUploadedFile(name, b"%PDF-1.4 sample cv", content_type="application/pdf")


@override_settings(MEDIA_ROOT=MEDIA)
class RecruitCase(TestCase):
    """A real company from seed_demo, so a hire can be paid through the actual payroll engine."""

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def setUp(self):
        call_command("seed_demo", verbosity=0)
        self.co = Company.objects.get()
        self.admin = get_user_model().objects.create_superuser("admin", password="x")
        self.client = Client()
        self.client.force_login(self.admin)
        self.vacancy = Vacancy.objects.create(company=self.co, title="Accountant", department="Finance")

    def applicant(self, **kw):
        kw.setdefault("first_name", "Ngozi")
        kw.setdefault("last_name", "Eze")
        kw.setdefault("email", "ngozi@example.test")
        return services.add_applicant(kw.pop("vacancy", self.vacancy), self.admin, **kw)

    def offer(self, applicant, accept=True, **kw):
        fields = dict(job_title="Accountant", gross_monthly=D("450000"), start_date=date.today() + timedelta(days=30),
                      employment_type="PERMANENT", probation_months=3, notes="")
        fields.update(kw)
        services.save_offer(applicant, self.admin, **fields)
        if accept:
            services.decide_offer(applicant, "accept", self.admin)
        applicant.refresh_from_db()
        return applicant

    def accepted(self, **kw):
        return self.offer(self.applicant(), **kw)


class ApplicantRuleTests(RecruitCase):
    def test_adding_records_the_application(self):
        a = self.applicant()
        self.assertEqual((a.stage, a.company, a.created_by), (Stage.APPLIED, self.co, self.admin))
        self.assertEqual([(e.stage, e.by) for e in a.events.all()], [(Stage.APPLIED, self.admin)])

    def test_one_application_per_email_per_vacancy(self):
        self.applicant()
        with self.assertRaises(ValueError) as ctx:
            self.applicant(email="NGOZI@example.test")                                     # any capitalisation
        self.assertIn("already applied", str(ctx.exception))
        other = Vacancy.objects.create(company=self.co, title="Clerk")
        self.applicant(vacancy=other)                                                      # a different job is fine
        self.applicant(first_name="A", email="")
        self.applicant(first_name="B", email="")                                           # blank emails never clash

    def test_only_open_vacancies_take_applications(self):
        for status in ("ON_HOLD", "FILLED", "CLOSED"):
            self.vacancy.status = status
            self.vacancy.save()
            with self.assertRaises(ValueError):
                self.applicant()

    def test_applicants_move_forward_only(self):
        a = self.applicant()
        services.move(a, Stage.SCREENING, self.admin, "Phone call")
        services.move(a, Stage.INTERVIEW, self.admin)
        self.assertEqual(a.stage, Stage.INTERVIEW)
        with self.assertRaises(ValueError):
            services.move(a, Stage.SCREENING)                                             # no going back
        with self.assertRaises(ValueError):
            services.move(a, Stage.INTERVIEW)                                             # already there
        for stage in (Stage.OFFER, Stage.HIRED, Stage.REJECTED):
            with self.assertRaises(ValueError):
                services.move(a, stage)                                                   # those have their own steps
        self.assertEqual([e.note for e in a.events.all()][1], "Phone call")

    def test_rejecting_needs_a_reason_and_is_final_until_reconsidered(self):
        a = self.applicant()
        with self.assertRaises(ValueError):
            services.reject(a, "   ")
        services.reject(a, "Not enough experience", self.admin)
        self.assertEqual((a.stage, a.rejection_reason), (Stage.REJECTED, "Not enough experience"))
        for action in (lambda: services.move(a, Stage.SCREENING), lambda: services.reject(a, "again"),
                       lambda: services.withdraw(a)):
            with self.assertRaises(ValueError):
                action()
        services.reopen(a, self.admin)
        self.assertEqual((a.stage, a.rejection_reason), (Stage.APPLIED, ""))
        with self.assertRaises(ValueError):
            services.reopen(a)                                                            # not rejected any more

    def test_cannot_reconsider_into_a_closed_vacancy(self):
        a = self.applicant()
        services.withdraw(a)
        self.vacancy.status = "CLOSED"
        self.vacancy.save()
        with self.assertRaises(ValueError):
            services.reopen(a)


class OfferTests(RecruitCase):
    def test_making_an_offer_moves_the_applicant_to_the_offer_stage(self):
        a = self.applicant()
        offer = services.save_offer(a, self.admin, job_title="Accountant", gross_monthly=D("450000"),
                                    start_date=date.today() + timedelta(days=30), employment_type="PERMANENT",
                                    probation_months=3, notes="")
        a.refresh_from_db()
        self.assertEqual((a.stage, offer.status, offer.company), (Stage.OFFER, "PENDING", self.co))
        self.assertEqual(a.events.last().note, "Offer made: Accountant.")
        services.save_offer(a, self.admin, job_title="Senior Accountant", gross_monthly=D("500000"),
                            start_date=offer.start_date, employment_type="PERMANENT", probation_months=3, notes="")
        a.refresh_from_db()
        self.assertEqual((Offer.objects.count(), a.offer.job_title, a.events.filter(stage=Stage.OFFER).count()),
                         (1, "Senior Accountant", 1))                                      # edited, not duplicated

    def test_offer_validation(self):
        a = self.applicant()
        base = dict(job_title="X", gross_monthly=D("1"), start_date=date.today(), employment_type="PERMANENT",
                    probation_months=0, notes="")
        with self.assertRaises(ValueError):
            services.save_offer(a, **{**base, "probation_months": 13})
        with self.assertRaises(ValueError):
            services.save_offer(a, **{**base, "start_date": a.applied_on - timedelta(days=1)})
        self.assertFalse(Offer.objects.exists())

    def test_the_candidates_reply(self):
        accepted = self.offer(self.applicant(), accept=True)
        self.assertEqual((accepted.stage, accepted.offer.status, accepted.offer.decided_on),
                         (Stage.OFFER, "ACCEPTED", date.today()))
        with self.assertRaises(ValueError):
            services.decide_offer(accepted, "accept")                                     # already answered
        with self.assertRaises(ValueError):
            services.save_offer(accepted, job_title="X", gross_monthly=D("1"), start_date=date.today(),
                                employment_type="PERMANENT", probation_months=0, notes="")   # locked once accepted

    def test_declining_ends_the_application(self):
        a = self.offer(self.applicant(), accept=False)
        services.decide_offer(a, "decline", self.admin)
        a.refresh_from_db()
        self.assertEqual((a.stage, a.offer.status), (Stage.WITHDRAWN, "DECLINED"))
        self.assertEqual(a.events.last().note, "Declined the offer.")

    def test_withdrawing_an_offer_allows_a_new_one(self):
        a = self.offer(self.applicant(), accept=False)
        services.decide_offer(a, "withdraw", self.admin)
        a.refresh_from_db()
        self.assertEqual((a.stage, a.offer.status), (Stage.INTERVIEW, "WITHDRAWN"))
        self.offer(a, gross_monthly=D("480000"))
        self.assertEqual((a.stage, a.offer.status, a.offer.gross_monthly), (Stage.OFFER, "ACCEPTED", D("480000.00")))

    def test_rejecting_closes_a_standing_offer(self):
        a = self.accepted()
        services.reject(a, "Reference check failed")
        a.refresh_from_db()
        self.assertEqual(a.offer.status, "WITHDRAWN")

    def test_unknown_decisions_and_missing_offers_are_refused(self):
        a = self.applicant()
        with self.assertRaises(ValueError):
            services.decide_offer(a, "accept")
        self.offer(a, accept=False)
        with self.assertRaises(ValueError):
            services.decide_offer(a, "maybe")


class HireTests(RecruitCase):
    def test_an_accepted_offer_becomes_an_employee(self):
        start = date(2026, 11, 30)
        a = self.accepted(start_date=start, gross_monthly=D("450000"), probation_months=3)
        emp = services.hire(a, "E900", self.admin, department="Accounts")
        self.assertEqual((emp.company, emp.employee_no, emp.first_name, emp.email, emp.department, emp.gross_monthly,
                          emp.is_active), (self.co, "E900", "Ngozi", "ngozi@example.test", "Accounts", D("450000"), True))
        a.refresh_from_db()
        self.assertEqual((a.stage, a.employee), (Stage.HIRED, emp))
        self.assertIn("Hired as E900", a.events.last().note)

    def test_the_department_defaults_to_the_vacancys(self):
        self.assertEqual(services.hire(self.accepted(), "E900").department, "Finance")

    @skipUnless(apps.is_installed("hr"), "the HR module is not installed")
    def test_it_creates_the_hr_record(self):
        from hr.models import EmployeeProfile
        a = self.accepted(start_date=date(2026, 11, 30), probation_months=3, employment_type="CONTRACT")
        a.phone = "0801"
        a.save()
        emp = services.hire(a, "E900")
        profile = EmployeeProfile.objects.get(employee=emp)
        self.assertEqual((profile.job_title, profile.employment_type, profile.hire_date, profile.phone),
                         ("Accountant", "CONTRACT", date(2026, 11, 30), "0801"))
        self.assertEqual(profile.probation_end_date, date(2027, 2, 28))                    # 30 Nov + 3 months, clamped

    @skipUnless(apps.is_installed("hr"), "the HR module is not installed")
    def test_no_probation_means_no_end_date(self):
        from hr.models import EmployeeProfile
        emp = services.hire(self.accepted(probation_months=0), "E900")
        self.assertIsNone(EmployeeProfile.objects.get(employee=emp).probation_end_date)

    def test_without_the_hr_module_only_the_employee_is_created(self):
        real = apps.is_installed
        with mock.patch("recruitment.services.apps.is_installed", side_effect=lambda n: n != "hr" and real(n)):
            emp = services.hire(self.accepted(), "E900")
        self.assertTrue(Employee.objects.filter(pk=emp.pk).exists())

    def test_an_onboarding_checklist_is_started(self):
        emp = services.hire(self.accepted(start_date=date(2026, 12, 1)), "E900")
        ob = emp.onboarding
        self.assertEqual((ob.status, ob.start_date, ob.applicant.first_name), ("OPEN", date(2026, 12, 1), "Ngozi"))
        names = [i.name for i in ob.items.all()]
        self.assertEqual(names[:len(services.DEFAULT_TASKS)], services.DEFAULT_TASKS)
        if apps.is_installed("portal"):
            self.assertEqual(names[-1], "Employee portal access given")
        real = apps.is_installed
        other = Vacancy.objects.create(company=self.co, title="Clerk")
        second = self.offer(self.applicant(vacancy=other, email="b@example.test"))
        with mock.patch("recruitment.services.apps.is_installed", side_effect=lambda n: n != "portal" and real(n)):
            plain = services.hire(second, "E901")
        self.assertEqual(plain.onboarding.items.count(), len(services.DEFAULT_TASKS))

    def test_refusals(self):
        pending = self.offer(self.applicant(), accept=False)
        with self.assertRaises(ValueError):
            services.hire(pending, "E900")                                                # not accepted yet
        with self.assertRaises(ValueError):
            services.hire(self.applicant(first_name="X", email="x@example.test"), "E900")  # no offer at all
        accepted = self.offer(self.applicant(first_name="Z", email="z@example.test"))
        with self.assertRaises(ValueError):
            services.hire(accepted, "  ")
        self.assertFalse(Employee.objects.filter(first_name="Z").exists())

    def test_employee_numbers_must_be_free(self):
        a = self.accepted()
        taken = Employee.objects.get(employee_no="E001").employee_no
        before = Employee.objects.count()
        with self.assertRaises(ValueError) as ctx:
            services.hire(a, taken.lower())
        self.assertIn("already in use", str(ctx.exception))
        self.assertEqual(Employee.objects.count(), before)                                 # nothing half-created
        self.assertFalse(Onboarding.objects.exists())

    def test_cannot_hire_twice(self):
        a = self.accepted()
        services.hire(a, "E900")
        with self.assertRaises(ValueError):
            services.hire(a, "E901")

    def test_the_vacancy_fills_when_its_positions_are_taken(self):
        self.vacancy.headcount = 2
        self.vacancy.save()
        services.hire(self.accepted(), "E900")
        self.vacancy.refresh_from_db()
        self.assertEqual(self.vacancy.status, "OPEN")                                      # one position still free
        services.hire(self.offer(self.applicant(first_name="Chi", email="chi@example.test")), "E901")
        self.vacancy.refresh_from_db()
        self.assertEqual(self.vacancy.status, "FILLED")
        with self.assertRaises(ValueError):
            self.applicant(first_name="Late", email="late@example.test")                    # no longer taking applications


class EmployeeNumberTests(RecruitCase):
    def test_follows_the_companys_own_style(self):
        self.assertEqual(services.suggest_employee_no(Company.objects.create(name="Empty")), "E001")
        other = Company.objects.create(name="Styled")
        for no in ("EMP-007", "EMP-006", "TEMP", "X-12"):
            Employee.objects.create(company=other, employee_no=no, first_name="A", last_name="B", gross_monthly=D("1"))
        self.assertEqual(services.suggest_employee_no(other), "X-13")                       # highest number wins
        Employee.objects.filter(company=other, employee_no="X-12").delete()
        self.assertEqual(services.suggest_employee_no(other), "EMP-008")

    def test_pads_to_the_existing_width(self):
        other = Company.objects.create(name="Padded")
        Employee.objects.create(company=other, employee_no="E099", first_name="A", last_name="B", gross_monthly=D("1"))
        self.assertEqual(services.suggest_employee_no(other), "E100")

    def test_month_arithmetic(self):
        self.assertEqual(services.add_months(date(2026, 11, 30), 3), date(2027, 2, 28))
        self.assertEqual(services.add_months(date(2026, 1, 31), 1), date(2026, 2, 28))
        self.assertEqual(services.add_months(date(2026, 10, 15), 12), date(2027, 10, 15))


class OnboardingRuleTests(RecruitCase):
    def test_completion_needs_every_item(self):
        ob = services.hire(self.accepted(), "E900").onboarding
        with self.assertRaises(ValueError) as ctx:
            services.complete_onboarding(ob, self.admin)
        self.assertIn("still to do", str(ctx.exception))
        ob.items.update(is_done=True)
        services.complete_onboarding(ob, self.admin)
        self.assertEqual((ob.status, ob.completed_by), ("COMPLETE", self.admin))
        with self.assertRaises(ValueError):
            services.complete_onboarding(ob, self.admin)


class PayrollTests(RecruitCase):
    @skipUnless(apps.is_installed("hr"), "start dates need the HR module")
    def test_a_mid_month_hire_is_paid_from_their_start_date(self):
        a = self.offer(self.applicant(applied_on=date(2026, 8, 15)), start_date=date(2026, 9, 20),
                       gross_monthly=D("300000"))
        emp = services.hire(a, "E900")
        september = PayrollRun.objects.create(company=self.co, year=2026, month=9)
        payroll_services.generate(september)
        slip = september.payslips.get(employee=emp)
        self.assertEqual(slip.gross, D("110000.00"))                                       # 300,000 x 11 / 30
        self.assertIn("11 of 30", slip.lines.get(code="PRORATA").name)
        self.assertEqual(september.payslips.get(employee__employee_no="E001").gross,
                         Employee.objects.get(employee_no="E001").gross_monthly)          # everyone else is unchanged

    @skipUnless(apps.is_installed("hr"), "start dates need the HR module")
    def test_a_future_hire_is_not_paid_yet(self):
        a = self.offer(self.applicant(applied_on=date(2026, 8, 15)), start_date=date(2026, 12, 1),
                       gross_monthly=D("300000"))
        emp = services.hire(a, "E900")
        september = PayrollRun.objects.create(company=self.co, year=2026, month=9)
        payroll_services.generate(september)
        self.assertFalse(september.payslips.filter(employee=emp).exists())
        december = PayrollRun.objects.create(company=self.co, year=2026, month=12)
        payroll_services.generate(december)
        self.assertEqual(december.payslips.get(employee=emp).gross, D("300000.00"))


class VacancyPageTests(RecruitCase):
    def test_login_is_required(self):
        resp = Client().get(reverse("recruitment:vacancies"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("login", resp["Location"])

    def test_create_edit_and_change_status(self):
        resp = self.client.post(reverse("recruitment:vacancy_new"), {
            "title": "Driver", "department": "Logistics", "employment_type": "CONTRACT", "headcount": "2"})
        vacancy = Vacancy.objects.get(title="Driver")
        self.assertRedirects(resp, reverse("recruitment:vacancy", args=[vacancy.pk]))
        self.assertEqual((vacancy.company, vacancy.created_by, vacancy.status), (self.co, self.admin, "OPEN"))
        self.client.post(reverse("recruitment:vacancy_edit", args=[vacancy.pk]), {
            "title": "Senior Driver", "department": "Logistics", "employment_type": "CONTRACT", "headcount": "3"})
        vacancy.refresh_from_db()
        self.assertEqual((vacancy.title, vacancy.headcount), ("Senior Driver", 3))
        bad = self.client.post(reverse("recruitment:vacancy_new"), {"title": "X", "headcount": "0",
                                                                    "employment_type": "PERMANENT"})
        self.assertEqual(bad.status_code, 200)
        self.client.post(reverse("recruitment:vacancy_status", args=[vacancy.pk, "ON_HOLD"]))
        vacancy.refresh_from_db()
        self.assertEqual(vacancy.status, "ON_HOLD")
        self.client.post(reverse("recruitment:vacancy_status", args=[vacancy.pk, "OPEN"]))
        self.assertEqual(Vacancy.objects.get(pk=vacancy.pk).status, "OPEN")
        self.assertContains(self.client.post(reverse("recruitment:vacancy_status", args=[vacancy.pk, "FILLED"]),
                                             follow=True), "Unknown status")               # filling happens by hiring
        self.assertEqual(self.client.get(reverse("recruitment:vacancy_status", args=[vacancy.pk, "OPEN"])).status_code, 405)

    def test_list_counts_and_filter(self):
        a = self.applicant()
        self.applicant(first_name="B", email="b@example.test")
        services.reject(a, "No")
        closed = Vacancy.objects.create(company=self.co, title="Old role", status="CLOSED")
        page = self.client.get(reverse("recruitment:vacancies"))
        row = [r for r in page.context["rows"] if r.pk == self.vacancy.pk][0]
        self.assertEqual((row.total, row.live, row.hired), (2, 1, 0))
        only = self.client.get(reverse("recruitment:vacancies"), {"status": "CLOSED"})
        self.assertEqual([r.pk for r in only.context["rows"]], [closed.pk])
        self.assertContains(page, ">Recruitment</a>")

    def test_the_pipeline_groups_applicants_by_stage(self):
        a, b = self.applicant(), self.applicant(first_name="Bayo", email="b@example.test")
        services.move(b, Stage.SCREENING)
        page = self.client.get(reverse("recruitment:vacancy", args=[self.vacancy.pk]))
        groups = {stage: members for stage, _, members in page.context["groups"]}
        self.assertEqual((groups[Stage.APPLIED], groups[Stage.SCREENING]), ([a], [b]))
        self.assertContains(page, "Bayo Eze")


class ApplicantPageTests(RecruitCase):
    def add(self, **extra):
        data = {"first_name": "Ngozi", "last_name": "Eze", "email": "ngozi@example.test", "source": "REFERRAL",
                "notes": "Strong CV", **extra}
        return self.client.post(reverse("recruitment:applicant_new", args=[self.vacancy.pk]), data)

    def test_adding_with_a_cv_stores_it_privately(self):
        resp = self.add(cv=pdf("Ngozi CV.pdf"))
        a = Applicant.objects.get()
        self.assertRedirects(resp, reverse("recruitment:applicant", args=[a.pk]))
        self.assertEqual((a.cv_name, a.source, a.created_by), ("Ngozi CV.pdf", "REFERRAL", self.admin))
        self.assertIn(f"recruitment/{self.co.pk}/", a.cv.name)
        self.assertNotIn("Ngozi", a.cv.name)                                                  # random stored name
        dl = self.client.get(reverse("recruitment:cv", args=[a.pk]))
        self.assertEqual(b"".join(dl.streaming_content), b"%PDF-1.4 sample cv")
        self.assertIn("attachment", dl["Content-Disposition"])
        self.assertIn("Ngozi CV.pdf", dl["Content-Disposition"])

    def test_adding_without_a_cv(self):
        self.add()
        a = Applicant.objects.get()
        self.assertFalse(a.cv)
        resp = self.client.get(reverse("recruitment:cv", args=[a.pk]), follow=True)
        self.assertContains(resp, "no CV on file")

    def test_bad_cvs_are_refused(self):
        for upload in (SimpleUploadedFile("run.exe", b"MZ"), SimpleUploadedFile("noext", b"%PDF-1.4"),
                       SimpleUploadedFile("fake.pdf", b"<html>not a pdf</html>"),
                       SimpleUploadedFile("fake.docx", b"%PDF-1.4")):
            self.assertEqual(self.add(cv=upload).status_code, 200, upload.name)
        self.assertFalse(Applicant.objects.exists())
        with mock.patch("recruitment.forms.MAX_UPLOAD_BYTES", 5):
            self.assertContains(self.add(cv=pdf()), "larger than")

    def test_a_duplicate_email_is_explained(self):
        self.add()
        self.assertContains(self.add(), "already applied")
        self.assertEqual(Applicant.objects.count(), 1)

    def test_editing_keeps_or_replaces_the_cv(self):
        self.add(cv=pdf("old.pdf"))
        a = Applicant.objects.get()
        old_path = a.cv.path
        edit = reverse("recruitment:applicant_edit", args=[a.pk])
        base = {"first_name": "Ngozi", "last_name": "Okafor", "email": "ngozi@example.test", "source": "REFERRAL"}
        self.client.post(edit, base)
        a.refresh_from_db()
        self.assertEqual((a.last_name, a.cv_name, a.cv.path), ("Okafor", "old.pdf", old_path))   # CV untouched
        self.client.post(edit, {**base, "cv": pdf("new.pdf")})
        a.refresh_from_db()
        self.assertEqual(a.cv_name, "new.pdf")
        other = self.applicant(first_name="Other", email="other@example.test")
        clash = self.client.post(reverse("recruitment:applicant_edit", args=[other.pk]), {**base, "first_name": "Other"})
        self.assertContains(clash, "already applied")

    def test_deleting_the_cv_or_the_applicant_removes_the_file(self):
        self.add(cv=pdf())
        a = Applicant.objects.get()
        path = a.cv.path
        self.assertTrue(os.path.exists(path))
        self.client.post(reverse("recruitment:cv_delete", args=[a.pk]))
        self.assertFalse(os.path.exists(path))
        a.refresh_from_db()
        self.assertEqual((bool(a.cv), a.cv_name), (False, ""))
        self.add(cv=pdf(), first_name="Second", email="second@example.test")
        second = Applicant.objects.get(first_name="Second")
        second_path = second.cv.path
        second.delete()
        self.assertFalse(os.path.exists(second_path))

    def test_stage_actions_through_the_pages(self):
        a = self.applicant()
        self.client.post(reverse("recruitment:applicant_move", args=[a.pk, "SCREENING"]), {"note": "Called"})
        a.refresh_from_db()
        self.assertEqual(a.stage, Stage.SCREENING)
        bad = self.client.post(reverse("recruitment:applicant_move", args=[a.pk, "OFFER"]), follow=True)
        self.assertContains(bad, "hiring follows an accepted offer")
        blank = self.client.post(reverse("recruitment:applicant_reject", args=[a.pk]), {"reason": ""}, follow=True)
        self.assertContains(blank, "Give a reason")
        self.client.post(reverse("recruitment:applicant_reject", args=[a.pk]), {"reason": "Too junior"})
        a.refresh_from_db()
        self.assertEqual((a.stage, a.rejection_reason), (Stage.REJECTED, "Too junior"))
        self.client.post(reverse("recruitment:applicant_reopen", args=[a.pk]))
        self.client.post(reverse("recruitment:applicant_withdraw", args=[a.pk]))
        a.refresh_from_db()
        self.assertEqual(a.stage, Stage.WITHDRAWN)
        self.assertEqual(self.client.get(reverse("recruitment:applicant_reject", args=[a.pk])).status_code, 405)
        page = self.client.get(reverse("recruitment:applicant", args=[a.pk]))
        self.assertContains(page, "Reconsider")
        self.assertContains(page, "Too junior")                                              # the history is kept

    def test_interviews(self):
        a = self.applicant()
        url = reverse("recruitment:interview_new", args=[a.pk])
        when = "2026-10-20T10:30"
        self.client.post(url, {"when": when, "interviewer": "Mrs Ade", "feedback": "Articulate", "rating": "4"})
        interview = Interview.objects.get()
        self.assertEqual((interview.applicant, interview.company, interview.rating), (a, self.co, 4))
        self.assertEqual(self.client.post(url, {"when": when, "interviewer": "X", "rating": "6"}).status_code, 200)
        self.client.post(reverse("recruitment:interview_edit", args=[interview.pk]),
                         {"when": when, "interviewer": "Mrs Ade", "feedback": "Updated"})
        interview.refresh_from_db()
        self.assertEqual((interview.feedback, interview.rating), ("Updated", None))
        self.assertContains(self.client.get(reverse("recruitment:applicant", args=[a.pk])), "Mrs Ade")
        self.client.post(reverse("recruitment:interview_delete", args=[interview.pk]))
        self.assertFalse(Interview.objects.exists())

    def test_offer_pages_and_the_hire(self):
        a = self.applicant()
        offer_url = reverse("recruitment:offer", args=[a.pk])
        form = self.client.get(offer_url).context["form"]
        self.assertEqual((form.initial["job_title"], form.initial["employment_type"]), ("Accountant", "PERMANENT"))
        start = (date.today() + timedelta(days=20)).isoformat()
        self.client.post(offer_url, {"job_title": "Accountant", "gross_monthly": "450000", "start_date": start,
                                     "employment_type": "PERMANENT", "probation_months": "3"})
        a.refresh_from_db()
        self.assertEqual((a.stage, a.offer.gross_monthly), (Stage.OFFER, D("450000.00")))
        page = self.client.get(reverse("recruitment:applicant", args=[a.pk]))
        self.assertContains(page, "They accepted")
        self.assertNotContains(page, "Hire Ngozi")
        blocked = self.client.post(reverse("recruitment:hire", args=[a.pk]), {"employee_no": "E900"}, follow=True)
        self.assertContains(blocked, "accepted an offer")
        self.client.post(reverse("recruitment:offer_decide", args=[a.pk, "accept"]))
        self.assertContains(self.client.get(reverse("recruitment:applicant", args=[a.pk])), "Hire Ngozi")
        hire_page = self.client.get(reverse("recruitment:hire", args=[a.pk]))
        self.assertEqual(hire_page.context["form"].initial["employee_no"], services.suggest_employee_no(self.co))
        self.assertContains(hire_page, "450,000.00")
        resp = self.client.post(reverse("recruitment:hire", args=[a.pk]), {"employee_no": "E900", "department": ""})
        emp = Employee.objects.get(employee_no="E900")
        self.assertRedirects(resp, reverse("recruitment:onboarding_detail", args=[emp.onboarding.pk]))
        self.assertEqual(emp.gross_monthly, D("450000.00"))

    def test_a_taken_employee_number_is_reported_and_nothing_is_created(self):
        a = self.accepted()
        resp = self.client.post(reverse("recruitment:hire", args=[a.pk]), {"employee_no": "E001"})
        self.assertContains(resp, "already in use")
        a.refresh_from_db()
        self.assertEqual(a.stage, Stage.OFFER)
        self.assertFalse(Onboarding.objects.exists())


class OnboardingPageTests(RecruitCase):
    def setUp(self):
        super().setUp()
        self.ob = services.hire(self.accepted(), "E900", self.admin).onboarding

    def test_list_and_detail(self):
        self.assertContains(self.client.get(reverse("recruitment:onboarding")), "0 of")
        page = self.client.get(reverse("recruitment:onboarding_detail", args=[self.ob.pk]))
        self.assertContains(page, "Employment contract signed")
        self.assertContains(page, "E900")

    def test_checklist_actions(self):
        item = self.ob.items.first()
        self.assertEqual(self.client.get(reverse("recruitment:item_toggle", args=[item.pk])).status_code, 405)
        self.client.post(reverse("recruitment:item_toggle", args=[item.pk]))
        item.refresh_from_db()
        self.assertEqual((item.is_done, item.done_by), (True, self.admin))
        self.client.post(reverse("recruitment:item_toggle", args=[item.pk]))
        item.refresh_from_db()
        self.assertEqual((item.is_done, item.done_by), (False, None))
        before = self.ob.items.count()
        self.client.post(reverse("recruitment:item_add", args=[self.ob.pk]), {"name": "Parking pass"})
        self.assertEqual(self.ob.items.count(), before + 1)
        self.client.post(reverse("recruitment:item_delete", args=[self.ob.items.last().pk]))
        self.assertEqual(self.ob.items.count(), before)

    def test_completing(self):
        blocked = self.client.post(reverse("recruitment:onboarding_complete", args=[self.ob.pk]), follow=True)
        self.assertContains(blocked, "still to do")
        self.ob.items.update(is_done=True)
        done = self.client.post(reverse("recruitment:onboarding_complete", args=[self.ob.pk]), follow=True)
        self.assertContains(done, "Onboarding complete")
        item = self.ob.items.first()
        again = self.client.post(reverse("recruitment:item_toggle", args=[item.pk]), follow=True)
        self.assertContains(again, "complete")
        item.refresh_from_db()
        self.assertTrue(item.is_done)                                                         # a finished list is locked


class IsolationTests(RecruitCase):
    def test_other_companies_are_unreachable(self):
        other = Company.objects.create(name="Beta Ltd")
        vacancy = Vacancy.objects.create(company=other, title="Secret role")
        theirs = services.add_applicant(vacancy, None, first_name="Zed", last_name="Zulu", email="zed@b.test",
                                        cv=pdf(), cv_name="zed.pdf")
        interview = Interview.objects.create(company=other, applicant=theirs, when="2026-10-20T10:00:00Z", interviewer="B")
        stranger = Employee.objects.create(company=other, employee_no="Z1", first_name="Zed", last_name="Zulu",
                                           gross_monthly=D("1"))
        ob = Onboarding.objects.create(company=other, employee=stranger, start_date=date.today())
        item = OnboardingItem.objects.create(company=other, onboarding=ob, name="Task")
        staff = get_user_model().objects.create_user("staff")
        CompanyAccess.objects.create(user=staff, company=self.co)
        c = Client()
        c.force_login(staff)
        gets = [("vacancy", [vacancy.pk]), ("vacancy_edit", [vacancy.pk]), ("applicant_new", [vacancy.pk]),
                ("applicant", [theirs.pk]), ("applicant_edit", [theirs.pk]), ("cv", [theirs.pk]),
                ("interview_new", [theirs.pk]), ("interview_edit", [interview.pk]), ("offer", [theirs.pk]),
                ("hire", [theirs.pk]), ("onboarding_detail", [ob.pk])]
        for name, args in gets:
            self.assertEqual(c.get(reverse(f"recruitment:{name}", args=args)).status_code, 404, name)
        posts = [("vacancy_status", [vacancy.pk, "CLOSED"]), ("applicant_move", [theirs.pk, "SCREENING"]),
                 ("applicant_reject", [theirs.pk]), ("applicant_withdraw", [theirs.pk]),
                 ("applicant_reopen", [theirs.pk]), ("cv_delete", [theirs.pk]), ("interview_delete", [interview.pk]),
                 ("offer_decide", [theirs.pk, "accept"]), ("item_add", [ob.pk]), ("item_toggle", [item.pk]),
                 ("item_delete", [item.pk]), ("onboarding_complete", [ob.pk])]
        for name, args in posts:
            self.assertEqual(c.post(reverse(f"recruitment:{name}", args=args)).status_code, 404, name)
        for name in ("vacancies", "onboarding"):
            page = c.get(reverse(f"recruitment:{name}"))
            self.assertNotContains(page, "Secret role")
            self.assertNotContains(page, "Zed")
        self.assertTrue(Applicant.objects.filter(pk=theirs.pk, stage="APPLIED").exists())


@skipUnless(apps.is_installed("audit"), "the audit log is not installed")
class AuditTests(RecruitCase):
    def test_a_hire_is_traced_and_the_cv_file_is_not_logged(self):
        from audit.models import AuditEntry
        self.client.post(reverse("recruitment:applicant_new", args=[self.vacancy.pk]), {
            "first_name": "Ngozi", "last_name": "Eze", "email": "n@example.test", "source": "OTHER", "cv": pdf()})
        a = Applicant.objects.get()
        created = AuditEntry.objects.get(model_label="recruitment.applicant", action="CREATE")
        self.assertEqual(created.who, "admin")
        self.assertNotIn("cv", {k.lower() for k in created.changes})
        self.assertNotIn(a.cv.name, str(created.changes))
        self.offer(a)
        self.client.post(reverse("recruitment:hire", args=[a.pk]), {"employee_no": "E900"})
        stage_changes = [e.changes["stage"] for e in AuditEntry.objects.filter(
            model_label="recruitment.applicant", action="UPDATE", changes__has_key="stage").order_by("id")]
        self.assertEqual(stage_changes, [["Applied", "Offer"], ["Offer", "Hired"]])
        self.assertTrue(AuditEntry.objects.filter(model_label="payroll.employee", action="CREATE",
                                                  object_repr__contains="E900", username="admin").exists())
        self.assertTrue(AuditEntry.objects.filter(model_label="recruitment.offer", action="CREATE").exists())
