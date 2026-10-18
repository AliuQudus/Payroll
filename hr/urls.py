from django.urls import path

from . import views

app_name = "hr"
urlpatterns = [
    path("", views.directory, name="directory"),
    path("alerts/", views.alerts, name="alerts"),
    path("e/<int:pk>/", views.detail, name="detail"),
    path("e/<int:pk>/record/", views.profile_edit, name="profile_edit"),
    path("e/<int:employee_pk>/contacts/new/", views.contact_edit, name="contact_new"),
    path("contacts/<int:pk>/edit/", views.contact_edit, name="contact_edit"),
    path("contacts/<int:pk>/delete/", views.contact_delete, name="contact_delete"),
    path("e/<int:employee_pk>/documents/new/", views.document_new, name="document_new"),
    path("documents/<int:pk>/download/", views.document_download, name="document_download"),
    path("documents/<int:pk>/delete/", views.document_delete, name="document_delete"),
]
