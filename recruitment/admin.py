from django.contrib import admin

from . import models

for m in (models.Vacancy, models.Applicant, models.Interview, models.Offer, models.Onboarding):
    admin.site.register(m)
