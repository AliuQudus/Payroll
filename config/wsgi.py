"""WSGI entry point for production servers, for example:  gunicorn config.wsgi:application"""
import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
application = get_wsgi_application()
