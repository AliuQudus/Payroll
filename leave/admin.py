from django.contrib import admin

from . import models

for m in (models.LeaveType, models.PublicHoliday, models.LeaveEntitlement, models.LeaveRequest,
          models.LeaveAllowanceApplication):
    admin.site.register(m)
