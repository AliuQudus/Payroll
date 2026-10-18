from django.urls import path

from . import views as v

app_name = "leave"
urlpatterns = [
    path("", v.requests, name="requests"),
    path("new/", v.new, name="new"),
    path("balances/", v.balances, name="balances"),
    path("s/<str:section>/", v.section_list, name="section_list"),
    path("s/<str:section>/new/", v.section_edit, name="section_new"),
    path("s/<str:section>/<int:pk>/edit/", v.section_edit, name="section_edit"),
    path("s/<str:section>/<int:pk>/delete/", v.section_delete, name="section_delete"),
    path("<int:pk>/", v.detail, name="detail"),
    path("<int:pk>/<str:action>/", v.action, name="action"),
]
