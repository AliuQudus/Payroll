import payroll.fields
from django.db import migrations

from payroll import encryption


def _rows(connection, table):
    qn = connection.ops.quote_name
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT id, smtp_password FROM {qn(table)} WHERE smtp_password <> ''")
        return cursor.fetchall()


def _store(connection, table, pk, value):
    qn = connection.ops.quote_name
    with connection.cursor() as cursor:
        cursor.execute(f"UPDATE {qn(table)} SET smtp_password = %s WHERE id = %s", [value, pk])


def encrypt_existing(apps, schema_editor):
    """Encrypt every mail password that was saved in plain text. Anything already encrypted is left alone."""
    connection, table = schema_editor.connection, apps.get_model("payroll", "Company")._meta.db_table
    for pk, value in _rows(connection, table):
        if not encryption.is_encrypted(value):
            _store(connection, table, pk, encryption.encrypt(value))


def decrypt_existing(apps, schema_editor):
    """Undo the above, so a rollback of this migration does not leave unreadable values in a plain text column."""
    connection, table = schema_editor.connection, apps.get_model("payroll", "Company")._meta.db_table
    for pk, value in _rows(connection, table):
        if encryption.is_encrypted(value):
            _store(connection, table, pk, encryption.decrypt(value))


class Migration(migrations.Migration):

    dependencies = [
        ('payroll', '0010_alter_approver_scope'),
    ]

    operations = [
        migrations.AlterField(
            model_name='company',
            name='smtp_password',
            field=payroll.fields.EncryptedCharField(blank=True, verbose_name='Mail password'),
        ),
        migrations.RunPython(encrypt_existing, decrypt_existing),
    ]
