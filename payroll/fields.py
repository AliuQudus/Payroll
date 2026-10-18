import logging

from django.db import models

from . import encryption

logger = logging.getLogger("payroll")


class EncryptedCharField(models.TextField):
    """Text that is encrypted in the database and plain in Python.

    - An empty value stays empty, so "nothing set" is visible without the key.
    - A value stored before encryption was added is still read (as it is) and is encrypted the next time it is saved.
    - If the key is wrong the value reads as empty and an error is logged, rather than stopping every page that loads
      the record. Setting it again repairs it.
    """
    description = "Text encrypted at rest"

    def from_db_value(self, value, expression, connection):
        if not encryption.is_encrypted(value):
            return value
        try:
            return encryption.decrypt(value)
        except encryption.DecryptionError:
            logger.error("Could not decrypt a stored %s. Was PAYROLL_ENCRYPTION_KEY changed? Enter the value again.",
                         self.name)
            return ""

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        return encryption.encrypt(value) if value else value
