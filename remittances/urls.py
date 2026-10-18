from django.urls import path

from . import views

app_name = "remittances"
urlpatterns = [
    path("", views.overview, name="overview"),
    path("deadlines/", views.deadlines, name="deadlines"),
    path("payments/<int:pk>/delete/", views.payment_delete, name="payment_delete"),
    path("<int:run_pk>/<slug:authority>/", views.schedule, name="schedule"),
    path("<int:run_pk>/<slug:authority>/csv/", views.schedule_csv, name="csv"),
    path("<int:run_pk>/<slug:authority>/pay/", views.payment_new, name="payment_new"),
]
