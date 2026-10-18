from django.contrib import admin

from . import models

for m in (models.EmployeeProfile, models.EmergencyContact, models.EmployeeDocument):
    admin.site.register(m)
