from django.conf import settings
from django.db import models


class AuditEntry(models.Model):
    """One thing that happened. Written by the system only: nothing in the app edits or deletes entries."""

    class Action(models.TextChoices):
        CREATE = "CREATE", "Created"
        UPDATE = "UPDATE", "Changed"
        DELETE = "DELETE", "Deleted"
        EVENT = "EVENT", "Event"
        LOGIN = "LOGIN", "Signed in"
        LOGIN_FAILED = "LOGIN_FAILED", "Failed sign-in"

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                             related_name="+")
    username = models.CharField(max_length=150, blank=True)           # kept even if the user is deleted
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    # Plain ids, not foreign keys: the record of a deleted company or employee must survive, and entries
    # written while something is being deleted must not point at a row that is about to disappear.
    company_id = models.PositiveIntegerField(null=True, blank=True)
    action = models.CharField(max_length=12, choices=Action.choices)
    model_label = models.CharField(max_length=60, blank=True)
    model_name = models.CharField(max_length=80, blank=True)
    object_id = models.CharField(max_length=40, blank=True)
    object_repr = models.CharField(max_length=200, blank=True)
    employee_id = models.PositiveIntegerField(null=True, blank=True)
    subject = models.CharField(max_length=200, blank=True)            # the employee's name at the time
    changes = models.JSONField(default=dict, blank=True)              # {field: [before, after]}

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name_plural = "audit entries"
        indexes = [models.Index(fields=["company_id", "-created_at"]), models.Index(fields=["employee_id"])]

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.username or 'system'} {self.get_action_display()} {self.object_repr}"

    @property
    def who(self):
        return self.username or "System"

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValueError("Audit entries cannot be changed.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("Audit entries cannot be deleted.")
