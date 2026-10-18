from django.db import migrations


def remove_levies(apps, schema_editor):
    """NHF, NSITF and ITF are no longer part of the payroll, so drop any saved rules for them."""
    apps.get_model("payroll", "StatutoryRule").objects.filter(code__in=["NHF", "NSITF", "ITF"]).delete()


class Migration(migrations.Migration):
    dependencies = [("payroll", "0006_alter_statutoryrule_code")]
    operations = [migrations.RunPython(remove_levies, migrations.RunPython.noop)]
