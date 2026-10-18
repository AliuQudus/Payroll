from django.contrib import admin

from . import models

for m in (models.RemittanceSetting, models.RemittancePayment):
    admin.site.register(m)
