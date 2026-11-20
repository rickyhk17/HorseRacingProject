from django.core.management.base import BaseCommand, CommandError

from notifications.commands_handler import ReceiverError, run_receiver
from notifications.services import ConfigurationError


class Command(BaseCommand):
    help = (
        "Run the authorized Telegram long-polling receiver. TELEGRAM_USER_ID must be a positive numeric "
        "user ID; TELEGRAM_BOT_TOKEN and positive numeric TELEGRAM_CHAT_ID come from the environment "
        "or private credentials file. Progress and completion notices use the outbox; due command "
        "notices are retried while running, but ambiguous sends are never retried. A succeeded command "
        "means the handler returned, not necessarily that collection succeeded. After a hard crash, "
        "verify the old worker is stopped before manually deleting TelegramPollLock(pk=1); interrupted "
        "commands become uncertain and are never automatically reexecuted. Polling stops if the last "
        "observed update is five days old, its timestamp is unavailable, or the cursor belongs to "
        "another bot or has no recorded bot ID: do not reset the offset or "
        "delete inbox history. Reconcile retained Telegram updates and side effects manually, then "
        "provision a new bot and clean receiver database, keeping the old database for audit. Resubmit "
        "only reviewed commands. Use a synchronized, trustworthy system clock."
    )

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Poll once and exit.")
        parser.add_argument("--poll-timeout", type=int, default=25)

    def handle(self, *args, **options):
        try:
            run_receiver(once=options["once"], poll_timeout=options["poll_timeout"])
        except (ReceiverError, ConfigurationError) as exc:
            raise CommandError(str(exc)) from None
        except Exception:
            raise CommandError("Telegram receiver failed; check the inbox and lock.") from None
