from django.db import migrations

PERCENT_RULES = ["PENSION_EMPLOYEE", "PENSION_EMPLOYER", "ITF", "RENT_RELIEF_RATE", "NHF", "NSITF"]


def to_percent(apps, schema_editor):
    """Rates used to be typed as fractions (0.15). They are now typed as percentages (15)."""
    TaxBand = apps.get_model("payroll", "TaxBand")
    StatutoryRule = apps.get_model("payroll", "StatutoryRule")
    groups = {}
    for band in TaxBand.objects.all():
        groups.setdefault((band.company_id, band.effective_from), []).append(band)
    for bands in groups.values():
        if max(b.rate for b in bands) <= 1:          # a whole set typed as fractions
            for b in bands:
                b.rate = b.rate * 100
                b.save(update_fields=["rate"])
    for rule in StatutoryRule.objects.filter(code__in=PERCENT_RULES):
        if 0 < rule.value < 1:
            rule.value = rule.value * 100
            rule.save(update_fields=["value"])


class Migration(migrations.Migration):
    dependencies = [("payroll", "0004_company_email_company_smtp_host_and_more")]
    operations = [migrations.RunPython(to_percent, migrations.RunPython.noop)]
