"""Checks whether this computer can reach a mail server, and optionally whether a login works.

Usage:  py check_mail.py                              connection test only, no password needed
        py check_mail.py you@gmail.com                also tests logging in (asks for your App Password)
        py check_mail.py you@gmail.com --debug        also shows the conversation with the server
        py check_mail.py you@example.com smtp.example.com
The password is never saved or printed, and the conversation hides anything that could contain it.
"""
import getpass
import smtplib
import ssl
import sys

debug = "--debug" in sys.argv
args = [a for a in sys.argv[1:] if not a.startswith("--")]
email = next((a for a in args if "@" in a), None)
host = next((a for a in args if "@" not in a), "smtp.gmail.com")
context = ssl.create_default_context()

VERBS = ("EHLO", "HELO", "STARTTLS", "MAIL", "RCPT", "DATA", "QUIT", "RSET", "NOOP", "AUTH")


def _safe_debug(self, *parts):
    text = " ".join(str(p) for p in parts)
    if text.startswith("send:") and not text[7:].upper().startswith(VERBS) or "AUTH" in text.upper()[:14]:
        text = "send: (login details hidden)"
    print("   ", text)


smtplib.SMTP._print_debug = _safe_debug


def attempt(label, action):
    try:
        action()
        print(f"OK    {label}")
        return True
    except Exception as exc:  # report every kind of failure
        print(f"FAIL  {label}  ->  {type(exc).__name__}: {exc}")
        return False


def connect(port, use_ssl, creds=None):
    if use_ssl:
        server = smtplib.SMTP_SSL(host, port, timeout=60, context=context)
    else:
        server = smtplib.SMTP(host, port, timeout=60)
    with server:
        if debug and creds:
            server.set_debuglevel(1)
        server.ehlo()
        if not use_ssl:
            server.starttls(context=context)
            server.ehlo()
        if creds:
            if "PLAIN" in server.esmtp_features.get("auth", "").split():
                server.esmtp_features["auth"] = "PLAIN"      # one method only, so the real error shows
            server.login(*creds)


print(f"Testing {host} ...\n")
a = attempt("Connect on port 587 with TLS", lambda: connect(587, False))
b = attempt("Connect on port 465 with SSL", lambda: connect(465, True))
print()
if not (a or b):
    print("This computer cannot complete a secure connection to the mail server.")
    print("Likely causes: antivirus email protection, a firewall, or your network blocking")
    print("outgoing mail. Turn off the antivirus email shield briefly, or try a phone hotspot.")
    sys.exit(1)
print("This computer can reach the mail server.")

if not email:
    print("To test a login too, run:  py check_mail.py you@gmail.com")
    sys.exit(0)

typed = getpass.getpass("\nApp Password (typing is hidden): ")
password = "".join(typed.split())          # removes ordinary and non-breaking spaces
print(f"Password received: {len(password)} characters (a Google App Password has 16).")
if len(password) != 16 and "gmail" in host:
    print("That is not 16 characters. The paste may have failed (in PowerShell, right-click to paste),")
    print("or this is not an App Password. Create a new one and try again.")
print()
creds = (email, password)
ok587 = attempt("Log in on port 587 (TLS)", lambda: connect(587, False, creds))
ok465 = attempt("Log in on port 465 (SSL)", lambda: connect(465, True, creds))
print()
if ok587:
    print("The login works on port 587. In Company settings use port 587 with Use TLS ticked.")
elif ok465:
    print("The login works on port 465. In Company settings set the port to 465.")
else:
    print("The login failed on both ports.")
    print("- A message mentioning credentials or an App Password: create a new App Password and retry.")
    print("- 'Connection unexpectedly closed' on both: something is closing the connection when it sees")
    print("  the login. Try a phone hotspot, and turn off antivirus email scanning for a moment.")
    print("Run again with --debug to see the conversation, and send me that output.")
