from django.contrib import admin

from . import models

for m in (models.PayGrade, models.SalaryRevision):
    admin.site.register(m)
