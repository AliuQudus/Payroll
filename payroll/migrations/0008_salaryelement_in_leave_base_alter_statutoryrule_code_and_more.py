from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('payroll', '0007_remove_old_levies'),
    ]

    operations = [
        migrations.AddField(
            model_name='salaryelement',
            name='in_leave_base',
            field=models.BooleanField(default=False, help_text='Counts toward the annual pay the leave allowance is calculated on, for example Basic.', verbose_name='leave allowance base'),
        ),
        migrations.AlterField(
            model_name='statutoryrule',
            name='code',
            field=models.CharField(choices=[('PENSION_EMPLOYEE', 'Pension rate (employee)'), ('PENSION_EMPLOYER', 'Pension rate (employer)'), ('RENT_RELIEF_RATE', 'Rent relief rate'), ('RENT_RELIEF_CAP', 'Rent relief annual cap'), ('ABSENCE_DIVISOR', 'Days used to value one day of absence (e.g. 30)'), ('LEAVE_ALLOWANCE', 'Leave allowance (% of annual pay)')], max_length=30),
        ),
    ]
