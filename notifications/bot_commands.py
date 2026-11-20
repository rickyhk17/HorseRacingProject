"""Telegram command dispatch: all collected material remains unverified."""

import os

from collection.racecard import collect_racecard
from collection.resultsall import CollectionError, collect_results
from racing.management.commands.run_racing_jobs import _dashboard_path

from .bot_prediction import candidate_summary


def handle_command(text):
    parts = text.split()
    if not parts:
        return "請輸入 /racedata、/result 或 /predit。"
    command = parts[0].lower()
    dashboard = _dashboard_path()
    suffix = f"\n核對網頁：{dashboard}" if dashboard and len(dashboard) <= 400 else "\n請登入私人管理網頁核對。"
    if command == "/predit":
        if len(parts) != 1:
            return "用法：/predit"
        return candidate_summary() + "\n僅使用已核實來源；Threads 和馬評資料需先人工核對。" + suffix
    if command not in {"/racedata", "/result"}:
        return "未知指令。可使用 /racedata、/result 或 /predit。"
    if len(parts) != 2:
        return f"用法：{command} 馬會網址"
    if os.environ.get("HKJC_TERMS_REVIEWED") != "yes":
        return "未確認馬會來源的自動存取及使用授權，無法採集。" + suffix
    url = parts[1].replace("&amp;", "&")
    if command == "/racedata":
        observation, _ = collect_racecard(url, terms_reviewed=True)
        if observation.status != "success":
            return f"賽事資料未能採集：{observation.error}。" + suffix
        return (
            f"排位 PDF 已保存為待核對紀錄 #{observation.pk}；"
            "內容僅為擷取文字，未自動辨識所有場次及馬匹，須人工核對。" + suffix
        )
    try:
        observations = collect_results(url, terms_reviewed=True)
    except CollectionError as exc:
        return f"賽果未能採集：{exc.code}。" + suffix
    return (
        f"已保存 {len(observations)} 場待核對賽果紀錄；"
        "未核實資料不會自動結算。請檢查漏場、派彩基準及特殊情況。" + suffix
    )
