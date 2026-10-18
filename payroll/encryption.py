"""Encrypting secrets that must be stored, such as a company's mail password.

Fernet gives authenticated encryption (AES with an integrity check), so a changed or truncated value is detected rather
than decrypted into nonsense. The keys come from settings.ENCRYPTION_KEYS (the environment), never from the database.
"""
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings

PREFIX = "enc1:"                    # marks a value as encrypted, and leaves room for a different scheme later


class DecryptionError(Exception):
    pass


@lru_cache(maxsize=8)
def _cipher(keys):
    return MultiFernet([Fernet(key) for key in keys])


def cipher():
    return _cipher(tuple(settings.ENCRYPTION_KEYS))


def is_encrypted(value):
    return isinstance(value, str) and value.startswith(PREFIX)


def encrypt(text):
    return PREFIX + cipher().encrypt(text.encode()).decode()


def decrypt(value):
    try:
        return cipher().decrypt(value[len(PREFIX):].encode()).decode()
    except (InvalidToken, ValueError) as exc:
        raise DecryptionError("The value could not be decrypted: wrong key, or it was changed.") from exc


def rotate(value):
    """The same secret re-encrypted with the newest key."""
    try:
        return PREFIX + cipher().rotate(value[len(PREFIX):].encode()).decode()
    except (InvalidToken, ValueError) as exc:
        raise DecryptionError("The value could not be decrypted: wrong key, or it was changed.") from exc
