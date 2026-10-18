from django.contrib import admin

from . import models

for m in (models.AttendanceSetting, models.AttendanceRecord, models.OvertimeRequest):
    admin.site.register(m)
