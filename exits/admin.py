from django.contrib import admin

from . import models

for m in (models.Exit, models.ClearanceItem, models.SettlementLine):
    admin.site.register(m)
