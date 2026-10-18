from django.urls import path

from . import views

app_name = "portal"
urlpatterns = [
    path("", views.home, name="home"),
    path("payslips/", views.payslips, name="payslips"),
    path("payslips/<int:pk>/", views.payslip, name="payslip"),
    path("leave/", views.leave, name="leave"),
    path("leave/new/", views.leave_new, name="leave_new"),
    path("leave/<int:pk>/cancel/", views.leave_cancel, name="leave_cancel"),
    path("attendance/", views.attendance, name="attendance"),
    path("overtime/", views.overtime, name="overtime"),
    path("overtime/new/", views.overtime_new, name="overtime_new"),
    path("overtime/<int:pk>/cancel/", views.overtime_cancel, name="overtime_cancel"),
    path("details/", views.details, name="details"),
    path("details/edit/", views.details_edit, name="details_edit"),
    path("details/contacts/new/", views.contact_edit, name="contact_new"),
    path("details/contacts/<int:pk>/edit/", views.contact_edit, name="contact_edit"),
    path("details/contacts/<int:pk>/delete/", views.contact_delete, name="contact_delete"),
    path("access/", views.access, name="access"),
    path("access/<int:pk>/grant/", views.access_grant, name="access_grant"),
    path("access/<int:pk>/invite/", views.access_invite, name="access_invite"),
    path("access/<int:pk>/revoke/", views.access_revoke, name="access_revoke"),
]
