from django.urls import path

from . import views as v

urlpatterns = [
    path("", v.home, name="home"),
    path("s/<str:section>/", v.section_list, name="section_list"),
    path("s/<str:section>/new/", v.section_edit, name="section_new"),
    path("s/<str:section>/<int:pk>/", v.section_view, name="section_view"),
    path("s/<str:section>/<int:pk>/edit/", v.section_edit, name="section_edit"),
    path("s/<str:section>/<int:pk>/delete/", v.section_delete, name="section_delete"),
    path("runs/", v.run_list, name="run_list"),
    path("runs/<int:pk>/", v.run_detail, name="run_detail"),
    path("runs/<int:pk>/bank.csv", v.run_csv, name="run_csv"),
    path("runs/<int:pk>/<str:action>/", v.run_action, name="run_action"),
    path("payslips/", v.payslip_list, name="payslip_list"),
    path("payslips/email/", v.payslip_email, name="payslip_email"),
    path("payslips/<int:pk>/", v.payslip_detail, name="payslip"),
    path("users/", v.user_list, name="user_list"),
    path("users/new/", v.user_edit, name="user_new"),
    path("users/<int:pk>/edit/", v.user_edit, name="user_edit"),
    path("approvers/", v.approver_list, name="approver_list"),
    path("approvers/<int:pk>/delete/", v.approver_delete, name="approver_delete"),
    path("calculator/", v.calculator, name="calculator"),
    path("account/", v.account, name="account"),
    path("account/theme/", v.set_theme, name="set_theme"),
    path("company/new/", v.company_new, name="company_new"),
    path("company/edit/", v.company_edit, name="company_edit"),
    path("company/test-email/", v.company_test_email, name="company_test_email"),
    path("company/switch/", v.company_switch, name="company_switch"),
]
