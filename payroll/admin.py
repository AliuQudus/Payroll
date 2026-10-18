from django.contrib import admin

from . import models


@admin.register(models.Company)
class CompanyAdmin(admin.ModelAdmin):
    """The mail password is set on the Company settings page, never shown or edited here, so the admin site cannot
    reveal it or accidentally blank it."""
    exclude = ("smtp_password",)


for m in (models.Employee, models.SalaryElement, models.EmployeeElementOverride,
          models.TaxBand, models.StatutoryRule, models.PayrollRun, models.Payslip, models.PayslipLine,
          models.EmployeeAdjustment, models.AdjustmentApplication, models.CompanyAccess, models.Approver):
    admin.site.register(m)
