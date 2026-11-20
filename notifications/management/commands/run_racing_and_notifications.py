"""Run racing alerts followed by the durable Telegram outbox."""

import math

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from notifications.models import JobRun
from notifications.services import ConfigurationError, process_notifications


class Command(BaseCommand):
    help = "Queue racing alerts, then send due Telegram notifications."

    def add_arguments(self, parser):
        parser.add_argument("--race-id", type=int, action="append", dest="race_ids")
        parser.add_argument("--lookback-hours", type=int, default=24)
        parser.add_argument("--lookahead-hours", type=int, default=24)
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--max-attempts", type=int, default=3)
        parser.add_argument("--retry-delay", type=int, default=300)
        parser.add_argument("--timeout", type=float, default=10)
        parser.add_argument("--lease-seconds", type=int, default=900)

    def handle(self, *args, **options):
        if (any(options[name] < 1 for name in ("limit", "max_attempts", "retry_delay", "lease_seconds"))
                or not math.isfinite(options["timeout"]) or options["timeout"] <= 0):
            raise CommandError("Invalid notification processing options.")
        try:
            call_command(
                "run_racing_jobs", race_ids=options["race_ids"],
                lookback_hours=options["lookback_hours"], lookahead_hours=options["lookahead_hours"],
                stdout=self.stdout, stderr=self.stderr, verbosity=options["verbosity"],
            )
        except Exception:
            raise CommandError("Racing jobs failed; notifications were not processed.") from None
        try:
            run = process_notifications(
                limit=options["limit"], max_attempts=options["max_attempts"],
                retry_delay=options["retry_delay"], timeout=options["timeout"],
                lease_seconds=options["lease_seconds"],
            )
        except ConfigurationError:
            raise CommandError(
                "Telegram credentials are missing, invalid, or the credentials file is unsafe."
            ) from None
        except Exception:
            raise CommandError("Notification processing failed; inspect JobRun.") from None
        summary = (
            f"JobRun {run.pk}: attempted={run.attempted} sent={run.sent} "
            f"retried={run.retried} failed={run.failed} uncertain={run.uncertain}"
        )
        if run.status == JobRun.Status.FAILED:
            raise CommandError(summary)
        self.stdout.write(summary)
