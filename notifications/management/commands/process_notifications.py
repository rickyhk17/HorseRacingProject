from django.core.management.base import BaseCommand, CommandError

from notifications.models import JobRun
from notifications.services import ConfigurationError, process_notifications


class Command(BaseCommand):
    help = "Send due Telegram outbox notifications and record the job outcome."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--max-attempts", type=int, default=3)
        parser.add_argument("--retry-delay", type=int, default=300, help="Retry delay in seconds.")
        parser.add_argument("--timeout", type=float, default=10, help="HTTP timeout in seconds.")
        parser.add_argument("--lease-seconds", type=int, default=900)

    def handle(self, *args, **options):
        try:
            run = process_notifications(
                limit=options["limit"], max_attempts=options["max_attempts"],
                retry_delay=options["retry_delay"], timeout=options["timeout"],
                lease_seconds=options["lease_seconds"],
            )
        except ConfigurationError:
            raise CommandError("Telegram credentials are missing or invalid.") from None
        except ValueError:
            raise CommandError("Invalid notification processing options.") from None
        except Exception:
            raise CommandError("Notification processing failed; inspect JobRun.") from None
        summary = (
            f"JobRun {run.pk}: attempted={run.attempted} sent={run.sent} "
            f"retried={run.retried} failed={run.failed} uncertain={run.uncertain}"
        )
        if run.status == JobRun.Status.FAILED:
            raise CommandError(summary)
        self.stdout.write(summary)
