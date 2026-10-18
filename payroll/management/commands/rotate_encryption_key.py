from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from payroll import encryption
from payroll.fields import EncryptedCharField


class Command(BaseCommand):
    help = ("Re-encrypt every encrypted value with the newest key. To replace a key: put the new key first in "
            "PAYROLL_ENCRYPTION_KEY and the old one in PAYROLL_ENCRYPTION_OLD_KEYS, run this, and only then "
            "remove the old key.")

    def handle(self, *args, **options):
        qn = connection.ops.quote_name
        done, failed = 0, []
        for model in apps.get_models():
            for field in (f for f in model._meta.concrete_fields if isinstance(f, EncryptedCharField)):
                table, column, pk = qn(model._meta.db_table), qn(field.column), qn(model._meta.pk.column)
                with transaction.atomic(), connection.cursor() as cursor:
                    cursor.execute(f"SELECT {pk}, {column} FROM {table} WHERE {column} <> ''")
                    for key, value in cursor.fetchall():
                        if not encryption.is_encrypted(value):
                            continue                           # an old plain value is encrypted by the next save
                        try:
                            fresh = encryption.rotate(value)
                        except encryption.DecryptionError:
                            failed.append(f"{model._meta.label}.{field.name} #{key}")
                            continue
                        cursor.execute(f"UPDATE {table} SET {column} = %s WHERE {pk} = %s", [fresh, key])
                        done += 1
        self.stdout.write(f"Re-encrypted {done} value(s) with the newest key.")
        if failed:
            raise CommandError(f"{len(failed)} value(s) could not be decrypted with any configured key and were "
                               f"left unchanged: {', '.join(failed[:10])}. Add the key they were made with to "
                               "PAYROLL_ENCRYPTION_OLD_KEYS.")
