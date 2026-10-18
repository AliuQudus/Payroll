from django.urls import path

from . import views

app_name = "salary"
urlpatterns = [
    path("", views.overview, name="overview"),
    path("grades/", views.grades, name="grades"),
    path("grades/new/", views.grade_edit, name="grade_new"),
    path("grades/<int:pk>/edit/", views.grade_edit, name="grade_edit"),
    path("grades/<int:pk>/delete/", views.grade_delete, name="grade_delete"),
    path("e/<int:pk>/", views.detail, name="detail"),
    path("e/<int:pk>/new/", views.revision_new, name="revision_new"),
    path("revisions/<int:pk>/delete/", views.revision_delete, name="revision_delete"),
]
