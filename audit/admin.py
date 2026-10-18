from django.contrib import admin

from .models import AuditEntry


@admin.register(AuditEntry)
class AuditEntryAdmin(admin.ModelAdmin):
    """Read-only: nobody edits the record, not even from the admin site."""
    list_display = ("created_at", "username", "action", "model_name", "object_repr")
    list_filter = ("action", "model_name")
    search_fields = ("username", "object_repr", "subject")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
