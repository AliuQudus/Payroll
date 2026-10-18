from django.urls import path

from . import views

app_name = "exits"
urlpatterns = [
    path("", views.overview, name="overview"),
    path("new/", views.new, name="new"),
    path("<int:pk>/", views.detail, name="detail"),
    path("<int:pk>/edit/", views.edit, name="edit"),
    path("<int:pk>/complete/", views.complete, name="complete"),
    path("<int:pk>/cancel/", views.cancel, name="cancel"),
    path("<int:pk>/suggest/", views.suggest, name="suggest"),
    path("<int:pk>/items/add/", views.item_add, name="item_add"),
    path("items/<int:pk>/toggle/", views.item_toggle, name="item_toggle"),
    path("items/<int:pk>/delete/", views.item_delete, name="item_delete"),
    path("<int:exit_pk>/lines/new/", views.line_edit, name="line_new"),
    path("lines/<int:pk>/edit/", views.line_edit, name="line_edit"),
    path("lines/<int:pk>/delete/", views.line_delete, name="line_delete"),
]
