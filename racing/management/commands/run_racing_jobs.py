"""Enqueue manual racing alerts; this command does not fetch official data."""

import os
import re
from datetime import timedelta
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import URLValidator
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from notifications.models import Notification
from notifications.services import enqueue_notification
from racing.models import Race
from racing.services import publication_cutoff, publish_prediction


def _dashboard_path():
    url = os.environ.get("RACING_DASHBOARD_URL", "")
    if url and not any(c in url for c in "?#\\") and not any(c.isspace() or ord(c) < 32 for c in url):
        try:
            parts = urlsplit(url)
            URLValidator(schemes=["https"])(url)
            if (parts.scheme == "https" and parts.hostname and parts.username is None
                    and parts.password is None and parts.port != 0
                    and not parts.query and not parts.fragment):
                return url
        except (ValidationError, ValueError):
            pass
    value = os.environ.get("RACING_DASHBOARD_PATH", "")
    # Only an unambiguous relative path; no authority, escaping, query or fragment.
    return value if re.fullmatch(r"/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]*", value) else ""


def _enqueue(key, text, dashboard_path):
    # Keep the first message stable even if optional configuration changes later.
    if Notification.objects.filter(idempotency_key=key).exists():
        return
    if dashboard_path:
        text += f"\nDashboard: {dashboard_path}"
    enqueue_notification(key, text)


def _selection_numbers(prediction):
    inputs = prediction.inputs
    selections = inputs.get("selections") if isinstance(inputs, dict) else None
    race_input = inputs.get("race") if isinstance(inputs, dict) else None
    meeting_input = inputs.get("meeting") if isinstance(inputs, dict) else None
    recommendations = inputs.get("recommendations") if isinstance(inputs, dict) else None
    if not isinstance(selections, list) or len(selections) != 4:
        return None
    if (not isinstance(race_input, dict) or not isinstance(race_input.get("starts_at"), str)
            or type(race_input.get("number")) is not int
            or not isinstance(meeting_input, dict) or type(meeting_input.get("id")) is not int
            or not isinstance(inputs.get("publication_cutoff"), str)
            or not isinstance(recommendations, list) or not recommendations):
        return None
    if any(not isinstance(item, dict) or type(item.get("id")) is not int
           or type(item.get("post_id")) is not int
           or not isinstance(item.get("match_verified_at"), str)
           or not isinstance(item.get("verified_at"), str) for item in recommendations):
        return None
    numbers = [item.get("number") if isinstance(item, dict) else None for item in selections]
    if any(type(number) is not int or not 1 <= number <= 32767 for number in numbers) or len(set(numbers)) != 4:
        return None
    ids = [item.get("id") for item in selections]
    if (any(type(pk) is not int or pk < 1 for pk in ids) or len(set(ids)) != 4
            or any(type(item.get("first_time")) is not bool for item in selections)):
        return None
    return numbers


@transaction.atomic
def _run_race(race_id, now, dashboard_path):
    race = Race.objects.select_for_update().select_related("meeting").get(pk=race_id)
    prefix = f"Race {race.pk}"
    prediction = race.predictions.order_by("-version").first()
    if prediction is None and not race.actual_bets.exists():
        return
    if now >= publication_cutoff(race) or race.predictions.filter(publication__isnull=False).exists():
        if prediction is None:
            _enqueue(
                f"racing:no-approval:{race.pk}",
                f"{prefix}: prediction approval is overdue. Manual review and official data are needed.",
                dashboard_path,
            )
        elif race.starts_at > now:
            numbers = _selection_numbers(prediction)
            if prediction.approved_at > now or numbers is None:
                _enqueue(
                    f"racing:invalid-prediction:{prediction.pk}",
                    f"{prefix}: approved prediction requires manual review; not published.",
                    dashboard_path,
                )
            else:
                try:
                    publish_prediction(prediction)
                except ValidationError:
                    _enqueue(
                        f"racing:publication-review:{prediction.pk}",
                        f"{prefix}: prediction v{prediction.version} could not be published; "
                        "manual review of eligibility and official data is needed.",
                        dashboard_path,
                    )
                else:
                    kind = "Prediction update" if prediction.version > 1 else "Approved prediction"
                    _enqueue(
                        f"racing:prediction:{prediction.pk}",
                        f"{prefix}: {kind} v{prediction.version}. Selections: "
                        + ", ".join(map(str, numbers))
                        + ". Informational only; no bet was placed.",
                        dashboard_path,
                    )
        elif not hasattr(prediction, "publication"):
            _enqueue(
                f"racing:missed-publication:{prediction.pk}",
                f"{prefix}: approved prediction was not published before the race start. "
                "Manual review is needed; no prediction was published late.",
                dashboard_path,
            )

    result = race.results.order_by("-version").first()
    if result and result.recorded_at <= now:
        inputs = result.inputs if isinstance(result.inputs, dict) else {}
        status = inputs.get("status")
        # Do not echo arbitrary notes, sources, credentials, or other free text.
        if not isinstance(status, str) or status not in {"pending", "manual", "final", "refund"}:
            status = "unknown"
        elif status in {"final", "refund"} and inputs.get("official_verified") is not True:
            status = "unknown"
        _enqueue(
            f"racing:result-update:{result.pk}",
            f"{prefix}: manually recorded result update v{result.version}; status {status}. "
            "Review official data in the dashboard.",
            dashboard_path,
        )
        if (result.actual_finished_at and result.actual_finished_at <= now - timedelta(minutes=30)
                and status not in {"final", "refund"}):
            _enqueue(
                f"racing:result-reminder:{result.pk}",
                f"{prefix}: 30 minutes have passed since the recorded finish. "
                "Manually obtain and verify official result data.",
                dashboard_path,
            )
    if race.starts_at <= now and (not result or result.recorded_at > now or not result.actual_finished_at):
        _enqueue(
            f"racing:manual-data:{race.pk}",
            f"{prefix}: race start has passed; manually check official data and record the actual finish time. "
            "No result was fetched.",
            dashboard_path,
        )


class Command(BaseCommand):
    help = "Enqueue due racing alerts. Official data must be entered manually; no bets are placed."

    def add_arguments(self, parser):
        parser.add_argument(
            "--race-id", type=int, action="append", dest="race_ids",
            help="Process only this race ID; repeat for multiple races, bypassing the default time window.",
        )
        parser.add_argument("--lookback-hours", type=int, default=24,
                            help="Default start-time window in the past (default: 24 hours).")
        parser.add_argument("--lookahead-hours", type=int, default=24,
                            help="Default start-time window in the future (default: 24 hours).")

    def handle(self, *args, **options):
        lookback = options["lookback_hours"]
        lookahead = options["lookahead_hours"]
        if not (0 <= lookback <= 8760 and 0 <= lookahead <= 8760):
            raise CommandError("Lookback and lookahead must be between 0 and 8760 hours.")
        now = timezone.now()
        race_ids = options["race_ids"]
        if race_ids is not None:
            if any(pk <= 0 for pk in race_ids):
                raise CommandError("Race IDs must be positive.")
            race_ids = sorted(set(race_ids))
            found = set(Race.objects.filter(pk__in=race_ids).values_list("pk", flat=True))
            if found != set(race_ids):
                raise CommandError("One or more race IDs do not exist; no races were processed.")
        else:
            race_ids = (Race.objects.filter(
                Q(predictions__isnull=False) | Q(actual_bets__isnull=False),
                starts_at__gte=now - timedelta(hours=lookback),
                starts_at__lte=now + timedelta(hours=lookahead),
            ).order_by("pk").values_list("pk", flat=True).distinct())
        dashboard_path = _dashboard_path()
        for race_id in race_ids:
            _run_race(race_id, now, dashboard_path)
        self.stdout.write("Racing alerts queued; official results are not fetched by this command.")
