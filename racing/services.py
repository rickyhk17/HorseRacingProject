"""Manual verification, immutable publication, and hypothetical settlement."""
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from itertools import combinations, permutations

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import Meeting, Prediction, Publication, Race, RaceResult, Runner, SourcePost, VerificationEvent


def race_eligibility(race):
    runners = list(race.runners.all())
    active = [r for r in runners if r.active]
    reasons = []
    if race.meeting.country != "HK" or not race.meeting.official_verified or not race.official_verified:
        reasons.append("Meeting and race must be officially verified in Hong Kong.")
    if race.is_griffin:
        reasons.append("Griffin races are excluded.")
    if len(active) < 9:
        reasons.append("At least nine active runners are required.")
    if sum(r.official_verified for r in active) < 9:
        reasons.append("At least nine active runners must be officially verified.")
    return reasons


def publication_cutoff(race):
    first = race.meeting.races.order_by("starts_at", "pk").first()
    return first.starts_at - timedelta(hours=1)


def eligible_recommendations(race):
    recommendations = race.recommendations.select_related("post", "runner").order_by("pk")
    earliest = race.starts_at - timedelta(hours=24)
    cutoff = publication_cutoff(race)
    return [
        rec for rec in recommendations
        if rec.runner.race_id == race.pk and rec.runner.active and rec.runner.official_verified
        and rec.official_match_verified and rec.verified_at and rec.verified_at < cutoff
        and rec.post.manually_verified and rec.post.published_at and rec.post.retrieved_at and rec.post.verified_at
        and (rec.post.source_type != SourcePost.SourceType.THREADS
             or (rec.post.followers is not None and rec.post.followers > 1000))
        and earliest <= rec.post.published_at < cutoff
        and rec.post.published_at <= rec.post.retrieved_at <= rec.post.verified_at < cutoff
        and rec.post.verified_at <= timezone.now() and rec.verified_at <= timezone.now()
    ]


def _source_key(post):
    return f"group:{post.syndication_key}" if post.syndication_key else f"post:{post.pk}"


def rank_candidates(races=None, limit=5):
    """Rank eligible future races by independent sources; never pad to a minimum."""
    if not 0 <= limit <= 5:
        raise ValidationError("Candidate limit must be between zero and five.")
    if races is None:
        races = Race.objects.select_related("meeting").all()
    candidates = []
    for race in races:
        if race.starts_at <= timezone.now() or race_eligibility(race):
            continue
        recommendations = eligible_recommendations(race)
        groups = {_source_key(rec.post) for rec in recommendations}
        if not groups:
            continue
        runner_groups = {}
        for rec in recommendations:
            runner_groups.setdefault(rec.runner_id, set()).add(_source_key(rec.post))
        runners = sorted(
            ({"runner": r, "score": len(runner_groups.get(r.pk, set()))}
             for r in race.runners.filter(active=True, official_verified=True)),
            key=lambda item: (-item["score"], item["runner"].number, item["runner"].pk),
        )
        candidates.append({"race": race, "score": len(groups), "runners": runners})
    candidates.sort(key=lambda item: (-item["score"], item["race"].starts_at, item["race"].pk))
    return candidates[:limit]


@transaction.atomic
def approve_prediction(race, runners, *, allow_late_revision=False):
    Meeting.objects.select_for_update().get(pk=race.meeting_id)
    race = Race.objects.select_for_update().select_related("meeting").get(pk=race.pk)
    errors = race_eligibility(race)
    previous = race.predictions.order_by("-version").first()
    if not previous and Prediction.objects.filter(race__meeting=race.meeting).values("race_id").distinct().count() >= 5:
        errors.append("At most five races per meeting may be approved.")
    cutoff = publication_cutoff(race)
    if timezone.now() >= cutoff and not (allow_late_revision and previous):
        errors.append("Initial approval must occur before the meeting publication cutoff; late revisions require explicit approval.")
    if race.starts_at <= timezone.now():
        errors.append("Approval must occur before the race.")
    if not race.approval_rationale.strip():
        errors.append("Record a rationale for the race and selected runners before approval.")
    selected_ids = [r.pk for r in runners]
    selected = list(race.runners.filter(pk__in=selected_ids, active=True, official_verified=True))
    if len(selected_ids) != 4 or len(set(selected_ids)) != 4 or len(selected) != 4:
        errors.append("Select exactly four distinct, verified active runners in this race.")
    recommendations = eligible_recommendations(race)
    if not recommendations:
        errors.append("At least one eligible independent recommendation is required.")
    if errors:
        raise ValidationError(errors)
    by_id = {r.pk: r for r in selected}
    selections = [by_id[pk] for pk in selected_ids]
    all_runners = list(race.runners.order_by("number"))
    inputs = {
        "meeting": {"id": race.meeting_id, "date": race.meeting.date.isoformat(), "venue": race.meeting.venue,
                    "country": race.meeting.country, "official_verified": race.meeting.official_verified},
        "race": {"number": race.number, "name": race.name, "starts_at": race.starts_at.isoformat(),
                 "official_verified": race.official_verified, "is_griffin": race.is_griffin},
        "publication_cutoff": cutoff.isoformat(),
        "approval_rationale": race.approval_rationale,
        "field": [{"id": r.pk, "number": r.number, "name": r.name, "active": r.active,
                   "official_verified": r.official_verified, "first_time": r.first_time} for r in all_runners],
        "selections": [{"id": r.pk, "number": r.number, "name": r.name, "first_time": r.first_time}
                       for r in selections],
        "first_timer_risk": any(r.first_time for r in selections),
        "recommendations": [
            {"id": rec.pk, "post_id": rec.post_id, "runner_id": rec.runner_id,
             "runner_number": rec.runner.number, "url": rec.post.url,
             "source_type": rec.post.source_type, "summary": rec.post.summary,
             "author": rec.post.author, "followers": rec.post.followers,
             "verifier": rec.post.verifier, "match_verifier": rec.verifier,
             "match_verified_at": rec.verified_at.isoformat(),
             "published_at": rec.post.published_at.isoformat(),
             "retrieved_at": rec.post.retrieved_at.isoformat(),
             "verified_at": rec.post.verified_at.isoformat(), "source_key": _source_key(rec.post)}
            for rec in recommendations
        ],
    }
    return Prediction.objects.create(race=race, version=previous.version + 1 if previous else 1,
                                     approved_at=timezone.now(), inputs=inputs)


@transaction.atomic
def publish_prediction(prediction):
    """Publish an already approved snapshot; safe for scheduler retries."""
    Meeting.objects.select_for_update().get(pk=prediction.race.meeting_id)
    prediction = Prediction.objects.select_for_update().select_related("race__meeting").get(pk=prediction.pk)
    existing = Publication.objects.filter(prediction=prediction).first()
    if existing:
        return existing
    race = prediction.race
    errors = race_eligibility(race)
    if race.predictions.order_by("-version").values_list("pk", flat=True).first() != prediction.pk:
        errors.append("Only the latest approved version can be published.")
    cutoff = publication_cutoff(race)
    if race.starts_at.isoformat() != prediction.inputs["race"]["starts_at"] or cutoff.isoformat() != prediction.inputs["publication_cutoff"]:
        errors.append("The race schedule or meeting publication cutoff changed after approval.")
    meeting_snapshot = prediction.inputs["meeting"]
    race_snapshot = prediction.inputs["race"]
    if (race.meeting_id != meeting_snapshot["id"] or race.number != race_snapshot["number"]
            or race.name != race_snapshot["name"] or race.meeting.venue != meeting_snapshot["venue"]
            or race.meeting.date.isoformat() != meeting_snapshot["date"]):
        errors.append("The official race identity changed after approval.")
    if timezone.now() < cutoff and not Publication.objects.filter(prediction__race=race).exists():
        errors.append("Initial publication must not occur before the meeting cutoff.")
    if timezone.now() >= race.starts_at:
        errors.append("Publication must occur before the race starts.")
    selections = prediction.inputs["selections"]
    ids = [item["id"] for item in selections]
    current = {r.pk: r for r in race.runners.filter(pk__in=ids, active=True, official_verified=True)}
    if (len(ids) != 4 or len(set(ids)) != 4 or len(current) != 4
            or any(current.get(item["id"]) is None or current[item["id"]].number != item["number"]
                   or current[item["id"]].name != item["name"]
                   or current[item["id"]].first_time != item["first_time"]
                   for item in selections)):
        errors.append("A selected runner is withdrawn, unverified, or has changed identity or first-timer status.")
    if VerificationEvent.objects.filter(subject="runner-active", subject_id__in=ids,
                                        verified=False, recorded_at__gt=prediction.approved_at).exists():
        errors.append("A selected runner was withdrawn after approval; manual review is required.")
    eligible = {rec.pk: rec for rec in eligible_recommendations(race)}
    for saved in prediction.inputs["recommendations"]:
        rec = eligible.get(saved["id"])
        if (rec is None or rec.post_id != saved["post_id"]
                or rec.verified_at.isoformat() != saved["match_verified_at"]
                or rec.post.verified_at.isoformat() != saved["verified_at"]):
            errors.append("An approved source or official match is no longer eligible.")
            break
    if not prediction.inputs["recommendations"]:
        errors.append("Publication requires an eligible recommendation.")
    published_races = Publication.objects.filter(prediction__race__meeting=race.meeting).values("prediction__race_id").distinct()
    if not published_races.filter(prediction__race_id=race.pk).exists() and published_races.count() >= 5:
        errors.append("At most five distinct races per meeting may be published.")
    if errors:
        raise ValidationError(errors)
    return Publication.objects.create(prediction=prediction, published_at=timezone.now())


def _money(value, name, *, positive=False):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValidationError(f"{name} must be a number.")
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise ValidationError(f"{name} must be {'positive' if positive else 'non-negative'}.")
    return result


def validate_result(race, status, finishing_numbers=None, dividend_base=None, tierce_dividend=None,
                    trio_dividend=None, official_verified=False, notes="", actual_finished_at=None):
    if status not in {"pending", "final", "refund", "manual"}:
        raise ValidationError("Invalid result status.")
    if status in {"final", "refund"} and not official_verified:
        raise ValidationError("Final results and refunds require manual official verification.")
    if status == "final" and timezone.now() < race.starts_at:
        raise ValidationError("Final results cannot be recorded before the scheduled start.")
    if status == "manual" and not notes.strip():
        raise ValidationError("Describe the dead heat, withdrawal, or other manual handling.")
    if actual_finished_at and (timezone.is_naive(actual_finished_at)
                               or actual_finished_at < race.starts_at or actual_finished_at > timezone.now()):
        raise ValidationError("Actual finish must be timezone-aware, after the scheduled start and not in the future.")
    numbers = list(finishing_numbers or [])
    if status == "final":
        if len(numbers) != 3 or len(set(numbers)) != 3 or any(not isinstance(n, int) or n < 1 for n in numbers):
            raise ValidationError("Enter three distinct finishing runner numbers; use manual for dead heats.")
        if race.runners.filter(number__in=numbers).count() != 3:
            raise ValidationError("Finishing runners must belong to this race.")
        base = _money(dividend_base, "Dividend base", positive=True)
        tierce = _money(tierce_dividend, "Tierce dividend")
        trio = _money(trio_dividend, "Trio dividend")
    else:
        if numbers or any(v is not None for v in (dividend_base, tierce_dividend, trio_dividend)):
            raise ValidationError("Only final results may specify finishers and dividends.")
        base = tierce = trio = None
    return {"status": status, "finishing_numbers": numbers, "dividend_base": str(base) if base is not None else None,
            "tierce_dividend": str(tierce) if tierce is not None else None,
            "trio_dividend": str(trio) if trio is not None else None,
            "official_verified": official_verified, "notes": notes,
            "actual_finished_at": actual_finished_at.isoformat() if actual_finished_at else None}


@transaction.atomic
def record_result(race, status, finishing_numbers=None, dividend_base=None, tierce_dividend=None,
                  trio_dividend=None, official_verified=False, notes="", actual_finished_at=None):
    race = Race.objects.select_for_update().get(pk=race.pk)
    inputs = validate_result(race, status, finishing_numbers, dividend_base, tierce_dividend,
                             trio_dividend, official_verified, notes, actual_finished_at)
    previous = race.results.order_by("-version").first()
    return RaceResult.objects.create(race=race, version=previous.version + 1 if previous else 1,
                                     recorded_at=timezone.now(), actual_finished_at=actual_finished_at, inputs=inputs)


def simulate_settlement(prediction, result=None):
    """Return hypothetical tickets, payout and ROI, using dividends per explicit base."""
    if result and result.race_id != prediction.race_id:
        raise ValidationError("Result and prediction must belong to the same race.")
    selected = [item["number"] for item in prediction.inputs["selections"]]
    if len(selected) != 4 or len(set(selected)) != 4:
        raise ValidationError("Prediction must contain four distinct runners.")
    inputs = result.inputs if result else {"status": "pending"}
    status = inputs["status"]
    selected_ids = [item["id"] for item in prediction.inputs["selections"]]
    if (Runner.objects.filter(race_id=prediction.race_id, pk__in=selected_ids, active=True).count() != 4
            or VerificationEvent.objects.filter(subject="runner-active", subject_id__in=selected_ids,
                                                verified=False, recorded_at__gt=prediction.approved_at).exists()):
        status = "manual"
    outcomes = {}
    for name, tickets, unit, dividend_name in (
        ("tierce", list(permutations(selected, 3)), Decimal("5"), "tierce_dividend"),
        ("trio", list(combinations(selected, 3)), Decimal("10"), "trio_dividend"),
        ("trio_same_budget", list(combinations(selected, 3)), Decimal("30"), "trio_dividend"),
    ):
        stake = unit * len(tickets)
        payout = None
        if status == "refund":
            payout = stake
        elif status == "final":
            finish = tuple(inputs["finishing_numbers"])
            win = finish in tickets if name == "tierce" else any(set(t) == set(finish) for t in tickets)
            payout = (Decimal(inputs[dividend_name]) * unit / Decimal(inputs["dividend_base"])) if win else Decimal(0)
        outcomes[name] = {"tickets": tickets, "unit_stake": unit, "stake": stake,
                          "payout": payout, "profit": payout - stake if payout is not None else None,
                          "roi": (payout - stake) / stake if payout is not None else None}
    return {"status": status, "reason": "Selected runner withdrawal requires ticket-specific manual settlement."
            if status == "manual" and inputs["status"] != "manual" else "",
            "prediction_version": prediction.version,
            "result_version": result.version if result else None, "outcomes": outcomes}
