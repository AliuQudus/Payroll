from django.conf import settings
from django.core.management.base import BaseCommand

from security import backup


class Command(BaseCommand):
    help = "Back up the database and the private media folder (HR documents and CVs) into one archive."

    def add_arguments(self, parser):
        parser.add_argument("--output-dir", default=settings.BACKUP_DIR, help="Where to write the archive.")
        parser.add_argument("--keep", type=int, default=None,
                            help="Afterwards delete the oldest backups so only this many remain.")
        parser.add_argument("--no-verify", action="store_true", help="Skip re-reading the archive after writing it.")

    def handle(self, *args, **options):
        path = backup.create_backup(options["output_dir"], keep=options["keep"], verify=not options["no_verify"])
        size = path.stat().st_size / (1024 * 1024)
        self.stdout.write(self.style.SUCCESS(f"Backup written: {path} ({size:.1f} MB)"
                                             + ("" if options["no_verify"] else ", verified")))
        self.stdout.write("It contains salaries and personal data: copy it off this server and keep it private.")
