"""Build a non-binding candidate summary from manually verified records."""

from django.utils import timezone

from racing.models import Race
from racing.services import eligible_recommendations, rank_candidates


def candidate_summary():
    next_race = (
        Race.objects.filter(starts_at__gt=timezone.now(), meeting__country="HK")
        .select_related("meeting")
        .order_by("starts_at", "pk")
        .first()
    )
    if not next_race:
        return "沒有未開始的香港賽事。請先核實並輸入賽事資料。"
    races = Race.objects.filter(meeting_id=next_race.meeting_id).select_related("meeting")
    candidates = []
    for race in races:
        ranked = rank_candidates([race])
        if ranked and sum(item["score"] > 0 for item in ranked[0]["runners"]) >= 4:
            candidates.append(ranked[0])
    candidates.sort(key=lambda item: (-item["score"], item["race"].starts_at, item["race"].pk))
    candidates = candidates[:5]
    lines = [
        f"候選預測（未批准）：{next_race.meeting.date} {next_race.meeting.venue}",
        "資料可能變動；須人工核實及批准，並非正式發布或下注指示。",
    ]
    included = 0
    for candidate in candidates:
        supported = [item for item in candidate["runners"] if item["score"] > 0]
        if len(supported) < 4:
            continue
        race = candidate["race"]
        selected = supported[:4]
        selected_ids = {item["runner"].pk for item in selected}
        recs = [rec for rec in eligible_recommendations(race) if rec.runner_id in selected_ids]
        sources = list(dict.fromkeys(rec.post.url for rec in recs))
        lines.append(
            f"第 {race.number} 場："
            + "、".join(f'{item["runner"].number} {item["runner"].name}' for item in selected)
        )
        lines.append(
            f'理由：{candidate["score"]} 組獨立已核實來源；按各馬來源數排序，'
            "同分按馬號。"
        )
        if any(item["runner"].first_time for item in selected):
            lines.append("風險：包含首次出賽馬匹。")
        lines.append("來源：" + "、".join(sources[:2]) + ("（其餘見網頁）" if len(sources) > 2 else ""))
        included += 1
    if not included:
        return (
            "資料不足：下一個賽日沒有四匹馬各有合資格已核實推介的場次。"
            "請先在網頁核實賽事及來源；不會補選馬匹。"
        )
    if included < 3:
        lines.append("合資格候選少於三場，不勉強投注。")
    text = "\n".join(lines)
    return text if len(text) <= 3500 else text[:3400] + "\n內容過長，請到網頁查看完整資料。"
