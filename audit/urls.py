from django.urls import path

from . import views

app_name = "audit"
urlpatterns = [
    path("", views.log, name="log"),
    path("export.csv", views.export, name="csv"),
]
