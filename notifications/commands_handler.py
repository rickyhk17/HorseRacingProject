"""Authorized, durable Telegram command inbox and long-polling worker."""

import json
import os
import re
from datetime import timedelta
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from .models import Notification, TelegramCommand, TelegramPollLock, TelegramPollState
from .services import _NoRedirect, _credentials, enqueue_notification, process_notifications


class ReceiverError(RuntimeError):
    """A safe-to-display receiver error."""


# Telegram retains incoming updates for at most 24 hours. Five days since
# observation therefore leaves at least a day before its seven-day reset.
MAX_IDLE = timedelta(days=5)


def _configuration():
    token, outgoing_chat = _credentials()
    bot_prefix = token.split(":", 1)[0]
    chat = outgoing_chat
    user = os.environ.get("TELEGRAM_USER_ID", "")
    if any(not re.fullmatch(r"[1-9][0-9]{0,18}", value) for value in (bot_prefix, chat, user)):
        raise ReceiverError("Bot, private chat and user IDs must be positive numeric values.")
    if any(int(value) > 2**63 - 1 for value in (bot_prefix, chat, user)):
        raise ReceiverError("Invalid Telegram bot, destination or user ID.")
    return token, int(chat), int(user), int(bot_prefix)


def _get_updates(token, offset, poll_timeout):
    request = Request(
        f"https://api.telegram.org/bot{token}/getUpdates",
        data=urlencode({
            "offset": offset, "timeout": poll_timeout,
            "allowed_updates": json.dumps(["message"]),
        }).encode("utf-8"),
        method="POST",
    )
    try:
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        with opener.open(request, timeout=poll_timeout + 10) as response:
            data = response.read(1024 * 1024 + 1)
            if response.status != 200 or len(data) > 1024 * 1024:
                raise ValueError
        body = json.loads(data)
        if not isinstance(body, dict) or body.get("ok") is not True or not isinstance(body.get("result"), list):
            raise ValueError
        updates = body["result"]
        if len(updates) > 100 or any(
            not isinstance(update, dict) or type(update.get("update_id")) is not int
            or not 0 <= update["update_id"] < 2**63 - 1
            for update in updates
        ):
            raise ValueError
        return updates
    except Exception:
        raise ReceiverError("Telegram polling failed.") from None


def _authorized_command(update, chat_id, user_id):
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    chat, sender = message.get("chat"), message.get("from")
    if (not isinstance(chat, dict) or chat.get("type") != "private"
            or type(chat.get("id")) is not int or chat["id"] != chat_id
            or not isinstance(sender, dict) or type(sender.get("id")) is not int
            or sender["id"] != user_id or sender.get("is_bot") is not False):
        return None
    text = message.get("text")
    if not isinstance(text, str) or not text or len(text) > 4096:
        return None
    text = text.strip()
    if not text or not re.fullmatch(r"/[A-Za-z0-9_]{1,32}", text.split(maxsplit=1)[0]):
        return None
    return text


def _store_updates(updates, chat_id, user_id, *, observed_at=None):
    """Persist all recognized commands and the acknowledgement offset atomically."""
    observed_at = observed_at or timezone.now()
    with transaction.atomic():
        state = TelegramPollState.objects.get(pk=1)
        offset = state.next_offset
        for update in sorted(updates, key=lambda item: item["update_id"]):
            update_id = update["update_id"]
            if update_id < offset:
                continue
            text = _authorized_command(update, chat_id, user_id)
            if text is not None:
                _, created = TelegramCommand.objects.get_or_create(update_id=update_id, defaults={"text": text})
                if not created:
                    raise ReceiverError("Update ID collision; stop and reconcile the inbox.")
            offset = update_id + 1
        if offset != state.next_offset:
            state.next_offset = offset
            state.last_update_at = observed_at
            state.save(update_fields=["next_offset", "last_update_at"])


def _safe_offset(state, now):
    if state.next_offset == 0:
        if TelegramCommand.objects.exists():
            raise ReceiverError("Inbox exists without a cursor; reconcile before polling.")
        return 0
    if (state.last_update_at is None or now < state.last_update_at
            or now - state.last_update_at >= MAX_IDLE):
        raise ReceiverError("Telegram cursor is too old or its timestamp is invalid; operator recovery is required.")
    return state.next_offset


def _notice(command, kind, text):
    return enqueue_notification(f"telegram-command:{command.update_id}:{kind}", text)


def _send_notice(notice):
    # A separate outbox worker can deliver pending notices if this attempt fails.
    try:
        process_notifications(limit=1, notification_id=notice.pk, timeout=5)
    except Exception:
        pass


def _flush_notices():
    pending = Notification.objects.filter(
        idempotency_key__startswith="telegram-command:",
    ).filter(
        Q(status=Notification.Status.PENDING, next_attempt_at__lte=timezone.now())
        | Q(status=Notification.Status.SENDING, claimed_at__lt=timezone.now() - timedelta(seconds=900))
    ).order_by("next_attempt_at", "pk")[:10]
    for notice in pending:
        _send_notice(notice)


def _recover_uncertain():
    notices = []
    with transaction.atomic():
        for command in TelegramCommand.objects.filter(status=TelegramCommand.Status.RUNNING):
            command.status = TelegramCommand.Status.UNCERTAIN
            command.save(update_fields=["status", "updated_at"])
            notices.append(_notice(command, "completion", "Command outcome is uncertain; it will not be run again automatically."))
    for notice in notices:
        _send_notice(notice)


def _execute_pending():
    for pk in TelegramCommand.objects.filter(status=TelegramCommand.Status.PENDING).order_by("update_id").values_list("pk", flat=True):
        with transaction.atomic():
            changed = TelegramCommand.objects.filter(pk=pk, status=TelegramCommand.Status.PENDING).update(
                status=TelegramCommand.Status.RUNNING, updated_at=timezone.now()
            )
            if not changed:
                continue
            command = TelegramCommand.objects.get(pk=pk)
            progress = _notice(command, "progress", "Command received; processing.")
        # Attempt this particular notice before running potentially slow network work.
        _send_notice(progress)
        try:
            from notifications.bot_commands import handle_command

            result = handle_command(command.text)
            if not isinstance(result, str) or not result or len(result) > 4096:
                raise ValueError
            status = TelegramCommand.Status.SUCCEEDED
        except Exception:
            result = "Command failed. It will not be run again automatically."
            status = TelegramCommand.Status.FAILED
        with transaction.atomic():
            completion = _notice(command, "completion", result)
            TelegramCommand.objects.filter(pk=pk, status=TelegramCommand.Status.RUNNING).update(
                status=status, updated_at=timezone.now()
            )
        _send_notice(completion)


def run_receiver(*, once=False, poll_timeout=25):
    """Run one worker; a crash leaves its lock for explicit operator recovery."""
    if not isinstance(poll_timeout, int) or not 1 <= poll_timeout <= 50:
        raise ReceiverError("Poll timeout must be between 1 and 50 seconds.")
    token, chat_id, user_id, bot_id = _configuration()
    try:
        with transaction.atomic():
            TelegramPollLock.objects.create(pk=1)
    except IntegrityError:
        raise ReceiverError("Receiver lock exists; verify the previous worker is stopped before removing it.") from None
    try:
        state, _ = TelegramPollState.objects.get_or_create(
            pk=1, defaults={"chat_id": chat_id, "user_id": user_id, "bot_id": bot_id}
        )
        if (state.chat_id, state.user_id, state.bot_id) != (chat_id, user_id, bot_id):
            raise ReceiverError("Receiver identity changed; reconcile the inbox and poll state before reconfiguring.")
        _recover_uncertain()
        while True:
            _safe_offset(TelegramPollState.objects.get(pk=1), timezone.now())
            _execute_pending()
            _flush_notices()
            observed_at = timezone.now()
            offset = _safe_offset(TelegramPollState.objects.get(pk=1), observed_at)
            updates = _get_updates(token, offset, poll_timeout)
            _store_updates(updates, chat_id, user_id, observed_at=observed_at)
            _execute_pending()
            if once:
                break
    finally:
        TelegramPollLock.objects.filter(pk=1).delete()
