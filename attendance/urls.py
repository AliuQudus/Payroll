from django.urls import path

from . import views

app_name = "attendance"
urlpatterns = [
    path("", views.register, name="register"),
    path("summary/", views.summary, name="summary"),
    path("summary.csv", views.summary_csv, name="summary_csv"),
    path("import/", views.import_view, name="import"),
    path("overtime/", views.overtime_list, name="overtime"),
    path("overtime/new/", views.overtime_new, name="overtime_new"),
    path("overtime/<int:pk>/", views.overtime_detail, name="overtime_detail"),
    path("overtime/<int:pk>/<str:action>/", views.overtime_decide, name="overtime_decide"),
    path("settings/", views.settings_view, name="settings"),
]
