from django.conf import settings
from django.db import models

from payroll.models import Employee


class EmployeeAccount(models.Model):
    """Links a login to one employee. That login can see that employee's own records and nothing else.

    The login is deliberately given no company access: everything in the payroll screens stays out of reach.
    """
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="employee_account")
    employee = models.OneToOneField(Employee, on_delete=models.CASCADE, related_name="portal_account")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    invited_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.employee} ({self.user.get_username()})"
