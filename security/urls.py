from django.urls import path

from . import views

app_name = "security"
urlpatterns = [
    path("healthz", views.health, name="health"),
    path("accounts/2fa/", views.setup, name="setup"),
    path("accounts/2fa/verify/", views.verify, name="verify"),
    path("accounts/2fa/disable/", views.disable, name="disable"),
    path("accounts/2fa/codes/", views.regenerate, name="regenerate"),
    path("accounts/2fa/users/", views.users, name="users"),
    path("accounts/2fa/users/<int:user_id>/reset/", views.reset, name="reset"),
]
