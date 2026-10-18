from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from security import backup


class Command(BaseCommand):
    help = ("Restore a backup made by the backup command (SQLite). STOP THE APP FIRST. The current database and "
            "media folder are renamed with .before-restore-<time>, not deleted.")

    def add_arguments(self, parser):
        parser.add_argument("archive")
        parser.add_argument("--yes", action="store_true", help="Confirm that you want to replace the current data.")
        parser.add_argument("--skip-media", action="store_true", help="Restore only the database.")

    def handle(self, *args, **options):
        database = settings.DATABASES["default"]
        if backup.engine_of(database) != "sqlite":
            raise CommandError("Automatic restore is for SQLite. For PostgreSQL use pg_restore (see DEPLOY.md).")
        if not options["yes"]:
            raise CommandError("This replaces the current database. Stop the app, then run again with --yes.")
        info = backup.restore_archive(options["archive"], database["NAME"], settings.MEDIA_ROOT,
                                      restore_media=not options["skip_media"])
        self.stdout.write(self.style.SUCCESS(
            f"Restored the backup made {info['created_at']} ({info['media_files']} media file(s))."))
        self.stdout.write(f"The previous data was kept alongside, with the ending {info['set_aside_suffix']}.")
        self.stdout.write("Now run 'python manage.py migrate' if this backup came from an older version, then start the app.")
