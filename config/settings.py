"""Settings. Development works with no configuration; production is switched on with PAYROLL_ENV=production.

Everything configurable is read from environment variables that start with PAYROLL_ (see DEPLOY.md). In production
the app refuses to start rather than run with a weak or missing setting.
"""
import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def env(name, default=None):
    return os.environ.get(f"PAYROLL_{name}", default)


def env_bool(name, default):
    value = env(name)
    return default if value is None else value.strip().lower() in ("1", "true", "yes", "on")


def env_int(name, default):
    value = env(name)
    try:
        return default if value is None else int(value)
    except ValueError:
        raise ImproperlyConfigured(f"PAYROLL_{name} must be a whole number, not '{value}'.")


def env_list(name, default):
    value = env(name)
    return default if value is None else [item.strip() for item in value.split(",") if item.strip()]


PRODUCTION = (env("ENV", "development") or "development").strip().lower() == "production"
DEBUG = env_bool("DEBUG", not PRODUCTION)
if PRODUCTION and DEBUG:
    raise ImproperlyConfigured("PAYROLL_DEBUG must be off in production: debug pages show settings and code.")

SECRET_KEY = env("SECRET_KEY")
if not SECRET_KEY:
    if PRODUCTION:
        raise ImproperlyConfigured("Set PAYROLL_SECRET_KEY to a long random string. Generate one with: "
                                   "python -c \"from django.core.management.utils import get_random_secret_key as g; print(g())\"")
    SECRET_KEY = "dev-only-change-me"
elif PRODUCTION and (len(SECRET_KEY) < 50 or len(set(SECRET_KEY)) < 10):
    raise ImproperlyConfigured("PAYROLL_SECRET_KEY is too short or too repetitive. Use at least 50 random characters.")

ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", [] if PRODUCTION else ["localhost", "127.0.0.1"])
if PRODUCTION and not ALLOWED_HOSTS:
    raise ImproperlyConfigured("Set PAYROLL_ALLOWED_HOSTS to your domain name(s), separated by commas. Invitation "
                               "emails and password links are built from it.")
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS", [])

# ---------------------------------------------------------------- encryption at rest
# Secrets stored in the database (each company's mail password, two-factor secrets) are encrypted with these keys, kept
# here and never in the database, so a copy of the database or a backup alone does not reveal them. The first key
# encrypts; the others are old keys that can still decrypt, so a key can be replaced without losing anything.
def _encryption_keys():
    import base64
    import hashlib
    primary = env("ENCRYPTION_KEY")
    if not primary:
        if PRODUCTION:
            raise ImproperlyConfigured(
                "Set PAYROLL_ENCRYPTION_KEY. Generate one with: python -c \"from cryptography.fernet import Fernet; "
                "print(Fernet.generate_key().decode())\"  Keep a copy somewhere safe and separate from your backups: "
                "without it the encrypted values cannot be read.")
        # development only: derive a key from the development secret so nothing needs configuring
        return [base64.urlsafe_b64encode(hashlib.sha256(f"payroll-dev-encryption:{SECRET_KEY}".encode()).digest()).decode()]
    keys = [primary] + env_list("ENCRYPTION_OLD_KEYS", [])
    from cryptography.fernet import Fernet
    for key in keys:
        try:
            Fernet(key)
        except Exception:
            raise ImproperlyConfigured("PAYROLL_ENCRYPTION_KEY and PAYROLL_ENCRYPTION_OLD_KEYS must be keys made by "
                                       "Fernet.generate_key() (44 characters, ending in =).")
    return keys


ENCRYPTION_KEYS = _encryption_keys()

INSTALLED_APPS = [
    "django.contrib.admin", "django.contrib.auth", "django.contrib.contenttypes",
    "django.contrib.sessions", "django.contrib.messages", "django.contrib.staticfiles", "django.contrib.humanize",
    "payroll", "leave", "hr", "recruitment", "salary", "exits", "attendance", "remittances", "portal", "audit",
    "security",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "security.middleware.TwoFactorMiddleware",      # remove together with "security" in INSTALLED_APPS
    "payroll.middleware.CompanyMiddleware",
    "audit.middleware.AuditMiddleware",     # remove together with "audit" in INSTALLED_APPS
]
ROOT_URLCONF = "config.urls"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [], "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
        "payroll.context.company",
    ]},
}]

# ---------------------------------------------------------------- database
DB_ENGINE = (env("DB_ENGINE", "sqlite") or "sqlite").strip().lower()
if DB_ENGINE == "sqlite":
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3",
                             "NAME": env("DB_PATH", str(BASE_DIR / "db.sqlite3")),
                             "OPTIONS": {"timeout": 20}}}               # wait for a lock instead of failing at once
elif DB_ENGINE in ("postgresql", "postgres"):
    if PRODUCTION and not (env("DB_NAME") and env("DB_USER")):
        raise ImproperlyConfigured("PostgreSQL needs PAYROLL_DB_NAME and PAYROLL_DB_USER (and usually "
                                   "PAYROLL_DB_PASSWORD and PAYROLL_DB_HOST).")
    DATABASES = {"default": {"ENGINE": "django.db.backends.postgresql", "NAME": env("DB_NAME", "payroll"),
                             "USER": env("DB_USER", ""), "PASSWORD": env("DB_PASSWORD", ""),
                             "HOST": env("DB_HOST", ""), "PORT": env("DB_PORT", ""),
                             "CONN_MAX_AGE": env_int("DB_CONN_MAX_AGE", 60)}}
else:
    raise ImproperlyConfigured(f"PAYROLL_DB_ENGINE must be 'sqlite' or 'postgresql', not '{DB_ENGINE}'.")

EMAIL_BACKEND = "payroll.mail.PayrollSMTPBackend"
LANGUAGE_CODE = "en-gb"
TIME_ZONE = "Africa/Lagos"
USE_TZ = True
STATIC_URL = "static/"
STATIC_ROOT = env("STATIC_ROOT", str(BASE_DIR / "staticfiles"))          # where collectstatic puts files for the web server
# HR documents and CVs live here. There is deliberately no MEDIA_URL: files are only served by an authenticated view,
# so the web server must never serve this folder.
MEDIA_ROOT = env("MEDIA_ROOT", str(BASE_DIR / "media"))
# Backups contain salaries and personal data, so keep this folder private (and copy the archives off the server).
BACKUP_DIR = env("BACKUP_DIR", str(BASE_DIR / "backups"))
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "login"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ---------------------------------------------------------------- sign-in protection (the security app)
AUTHENTICATION_BACKENDS = ["security.backends.LockoutBackend"]      # replaces the default so every sign-in is covered
LOGIN_MAX_FAILURES = env_int("LOGIN_MAX_FAILURES", 5)                # wrong passwords for one username before it locks
LOGIN_LOCKOUT_MINUTES = env_int("LOGIN_LOCKOUT_MINUTES", 15)
LOGIN_MAX_FAILURES_PER_IP = env_int("LOGIN_MAX_FAILURES_PER_IP", 25)  # across all usernames, from one address
# Name of the request header that carries the visitor's real address when you run behind a proxy that sets it,
# for example HTTP_X_REAL_IP. Leave unset otherwise: a header the visitor can set themselves must not be trusted.
CLIENT_IP_HEADER = env("CLIENT_IP_HEADER")

# Two-factor sign-in with an authenticator app. Anyone can switch it on for themselves; set PAYROLL_REQUIRE_2FA_FOR_ADMINS
# to make it compulsory for administrators.
TWO_FACTOR_REQUIRED_FOR_ADMINS = env_bool("REQUIRE_2FA_FOR_ADMINS", False)
TWO_FACTOR_ISSUER = env("TWO_FACTOR_ISSUER", "Payroll")      # the name the authenticator app shows

# ---------------------------------------------------------------- sessions and transport
SESSION_COOKIE_AGE = env_int("SESSION_HOURS", 12) * 3600
SESSION_SAVE_EVERY_REQUEST = True                                    # so the age above is an idle timeout
SESSION_EXPIRE_AT_BROWSER_CLOSE = env_bool("SESSION_EXPIRE_AT_BROWSER_CLOSE", False)
SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = env_bool("SECURE_COOKIES", PRODUCTION)
SESSION_COOKIE_SAMESITE = CSRF_COOKIE_SAMESITE = "Lax"
SECURE_SSL_REDIRECT = env_bool("SSL_REDIRECT", PRODUCTION)
SECURE_REDIRECT_EXEMPT = [r"^healthz$"]                              # so a load balancer can check over plain http
SECURE_PROXY_SSL_HEADER = None
if env_bool("BEHIND_PROXY", False):                                  # only when a proxy you control sets this header
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_HSTS_SECONDS = env_int("HSTS_SECONDS", 3600 if PRODUCTION else 0)   # raise to a year once https is settled
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("HSTS_INCLUDE_SUBDOMAINS", False)
SECURE_HSTS_PRELOAD = env_bool("HSTS_PRELOAD", False)
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "SAMEORIGIN"                                       # the calculator page frames a file from this site
# Django prefers DENY (security.W019). SAMEORIGIN still stops every other site from framing these pages, which is
# the clickjacking risk; it is kept only because the calculator embeds a page from this same site.
SILENCED_SYSTEM_CHECKS = ["security.W019"]

# ---------------------------------------------------------------- logging
LOGGING = {
    "version": 1, "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "loggers": {name: {"handlers": ["console"], "level": env("LOG_LEVEL", "INFO"), "propagate": False}
                for name in ("audit", "security", "payroll")}
              | {"django.security": {"handlers": ["console"], "level": "WARNING", "propagate": False}},
}
