from decimal import Decimal
from functools import wraps

from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Count
from django.http import HttpResponseForbidden
from django.shortcuts import render

from notifications.models import JobRun, Notification

from .models import ActualBet, Prediction, RaceResult
from .services import rank_candidates, simulate_settlement


def _totals():
    return {
        "stake": Decimal("0"), "payout": Decimal("0"), "count": 0,
        "winning_races": 0, "profitable_races": 0,
    }


def _finish_totals(total):
    total["profit"] = total["payout"] - total["stake"]
    total["roi_percent"] = (
        total["profit"] / total["stake"] * 100 if total["stake"] else None
    )
    return total


def superuser_only(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if request.user.is_authenticated and not (request.user.is_active and request.user.is_superuser):
            return HttpResponseForbidden()
        return view(request, *args, **kwargs)

    return wrapped


@superuser_only
@staff_member_required
def dashboard(request):
    predictions = Prediction.objects.filter(
        publication__isnull=False
    ).select_related("race__meeting", "publication").order_by(
        "publication__published_at", "publication__pk"
    )
    result_by_race = {}
    for result in RaceResult.objects.order_by("race_id", "-version"):
        result_by_race.setdefault(result.race_id, result)

    simulations = []
    totals = {name: _totals() for name in ("tierce", "trio", "trio_same_budget")}
    hits = 0
    final_races = 0
    seen_races = set()
    for prediction in predictions:
        if prediction.race_id in seen_races:
            continue
        seen_races.add(prediction.race_id)
        result = result_by_race.get(prediction.race_id)
        settlement = simulate_settlement(prediction, result)
        if settlement["status"] == "final":
            final_races += 1
            selected = {runner["number"] for runner in prediction.inputs["selections"]}
            if set(result.inputs["finishing_numbers"]).issubset(selected):
                hits += 1
        for name, outcome in settlement["outcomes"].items():
            if outcome["payout"] is not None:
                total = totals[name]
                total["stake"] += outcome["stake"]
                total["payout"] += outcome["payout"]
                total["count"] += 1
                if settlement["status"] == "final":
                    total["winning_races"] += outcome["payout"] > 0
                    total["profitable_races"] += outcome["payout"] > outcome["stake"]
        simulations.append({"prediction": prediction, "settlement": settlement})

    actual = _totals()
    pending_bets = 0
    for bet in ActualBet.objects.all():
        if bet.payout is None:
            pending_bets += 1
            continue
        actual["stake"] += bet.stake
        actual["payout"] += bet.payout + (bet.refund_amount or Decimal("0"))
        actual["count"] += 1

    counts = dict(Notification.objects.values_list("status").annotate(count=Count("pk")))
    return render(request, "racing/dashboard.html", {
        "candidates": rank_candidates(),
        "simulations": simulations,
        "tierce": _finish_totals(totals["tierce"]),
        "trio": _finish_totals(totals["trio"]),
        "trio_same_budget": _finish_totals(totals["trio_same_budget"]),
        "hit_count": hits,
        "final_races": final_races,
        "hit_rate_percent": Decimal(hits) / final_races * 100 if final_races else None,
        "actual": _finish_totals(actual),
        "pending_bets": pending_bets,
        "notification_counts": [
            {"label": label, "count": counts.get(value, 0)}
            for value, label in Notification.Status.choices
        ],
        "latest_job": JobRun.objects.order_by("-started_at", "-pk").first(),
    })
