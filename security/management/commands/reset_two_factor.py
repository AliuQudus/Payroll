from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from security import twofactor
from security.sessions import end_sessions


class Command(BaseCommand):
    help = ("Switch off a person's two-factor sign-in and sign them out everywhere. For when an administrator has lost "
            "their phone and recovery codes and no other administrator can use the Reset button. They can then sign in "
            "with their password and set it up again.")

    def add_arguments(self, parser):
        parser.add_argument("username")

    def handle(self, *args, **options):
        User = get_user_model()
        try:
            user = User.objects.get(**{User.USERNAME_FIELD: options["username"]})
        except User.DoesNotExist:
            raise CommandError(f"There is no user named '{options['username']}'.")
        had = twofactor.is_enabled(user)
        twofactor.disable(user)
        ended = end_sessions(user)
        twofactor.audit("Two-factor reset from the command line", user.get_username())
        self.stdout.write(self.style.SUCCESS(
            f"Two-factor sign-in was reset for {user.get_username()}" + ("" if had else " (it was not switched on)")
            + f". {ended} open session(s) ended."))
