from django.contrib import admin

from .models import LoginFailure


@admin.register(LoginFailure)
class LoginFailureAdmin(admin.ModelAdmin):
    """Read-only: a record of wrong passwords, kept for a day."""
    list_display = ("created_at", "username", "ip_address")
    search_fields = ("username", "ip_address")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
