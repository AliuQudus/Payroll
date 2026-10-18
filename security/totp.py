"""Time-based one-time passwords (RFC 6238): the six-digit codes shown by authenticator apps. Standard library only."""
import base64
import hmac
import os
import struct
import time
from urllib.parse import quote

STEP = 30            # seconds a code lasts
DIGITS = 6


def new_secret():
    """160 random bits as base32, the format authenticator apps expect."""
    return base64.b32encode(os.urandom(20)).decode().rstrip("=")


def _key(secret):
    secret = secret.strip().replace(" ", "").upper()
    return base64.b32decode(secret + "=" * (-len(secret) % 8))


def code_for_step(secret, step):
    mac = hmac.new(_key(secret), struct.pack(">Q", step), "sha1").digest()
    offset = mac[-1] & 0x0F
    number = (struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** DIGITS)
    return f"{number:0{DIGITS}d}"


def current_step(now=None):
    return int((time.time() if now is None else now) // STEP)


def code_at(secret, now=None):
    return code_for_step(secret, current_step(now))


def verify(secret, code, last_step=0, now=None, window=1):
    """The time step the code belongs to, or None. Steps at or before last_step are refused, so a code works once.

    A window of 1 also accepts the step before and after, which covers a phone clock that is a little off.
    """
    code = (code or "").strip().replace(" ", "")
    if len(code) != DIGITS or not (code.isascii() and code.isdigit()):      # str.isdigit() also accepts other scripts
        return None
    if not (secret or "").strip():
        return None             # a missing secret (for example, unreadable after a lost key) must never accept a code
    now_step, found = current_step(now), None
    try:
        for step in range(now_step - window, now_step + window + 1):
            if hmac.compare_digest(code_for_step(secret, step), code) and step > last_step:
                found = step                              # keep looping: every candidate takes the same time
    except ValueError:                                    # not valid base32: no code can match it
        return None
    return found


def provisioning_uri(secret, username, issuer):
    label = quote(f"{issuer}:{username}", safe="")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer, safe='')}&algorithm=SHA1&digits={DIGITS}&period={STEP}"
