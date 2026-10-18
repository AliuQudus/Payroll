from django.urls import path

from . import views

app_name = "recruitment"
urlpatterns = [
    path("", views.vacancies, name="vacancies"),
    path("vacancies/new/", views.vacancy_new, name="vacancy_new"),
    path("vacancies/<int:pk>/", views.vacancy_detail, name="vacancy"),
    path("vacancies/<int:pk>/edit/", views.vacancy_edit, name="vacancy_edit"),
    path("vacancies/<int:pk>/status/<str:status>/", views.vacancy_status, name="vacancy_status"),
    path("vacancies/<int:vacancy_pk>/applicants/new/", views.applicant_new, name="applicant_new"),
    path("applicants/<int:pk>/", views.applicant_detail, name="applicant"),
    path("applicants/<int:pk>/edit/", views.applicant_edit, name="applicant_edit"),
    path("applicants/<int:pk>/move/<str:stage>/", views.applicant_move, name="applicant_move"),
    path("applicants/<int:pk>/reject/", views.applicant_reject, name="applicant_reject"),
    path("applicants/<int:pk>/withdraw/", views.applicant_withdraw, name="applicant_withdraw"),
    path("applicants/<int:pk>/reopen/", views.applicant_reopen, name="applicant_reopen"),
    path("applicants/<int:pk>/cv/", views.cv_download, name="cv"),
    path("applicants/<int:pk>/cv/delete/", views.cv_delete, name="cv_delete"),
    path("applicants/<int:applicant_pk>/interviews/new/", views.interview_edit, name="interview_new"),
    path("interviews/<int:pk>/edit/", views.interview_edit, name="interview_edit"),
    path("interviews/<int:pk>/delete/", views.interview_delete, name="interview_delete"),
    path("applicants/<int:pk>/offer/", views.offer_edit, name="offer"),
    path("applicants/<int:pk>/offer/<str:decision>/", views.offer_decide, name="offer_decide"),
    path("applicants/<int:pk>/hire/", views.hire, name="hire"),
    path("onboarding/", views.onboarding_list, name="onboarding"),
    path("onboarding/<int:pk>/", views.onboarding_detail, name="onboarding_detail"),
    path("onboarding/<int:pk>/items/add/", views.item_add, name="item_add"),
    path("onboarding/<int:pk>/complete/", views.onboarding_complete, name="onboarding_complete"),
    path("onboarding/items/<int:pk>/toggle/", views.item_toggle, name="item_toggle"),
    path("onboarding/items/<int:pk>/delete/", views.item_delete, name="item_delete"),
]
