"""Durable, conservative Telegram outbox processing."""

import json
import re
import uuid
from datetime import timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from django.db.models import F
from django.utils import timezone

from .credentials import CredentialsFileError, credential_values
from .models import JobRun, Notification


class ConfigurationError(ValueError):
    pass


class IdempotencyConflict(ValueError):
    pass


class ProcessingError(RuntimeError):
    pass


def enqueue_notification(idempotency_key, text):
    """Create one outbox item per key; reject accidental reuse with different text."""
    if not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 255:
        raise ValueError("idempotency_key must be a non-empty string of at most 255 characters")
    if not isinstance(text, str) or not text or len(text) > 4096:
        raise ValueError("text must be a non-empty string of at most 4096 characters")
    notification, created = Notification.objects.get_or_create(
        idempotency_key=idempotency_key, defaults={"text": text}
    )
    if not created and notification.text != text:
        raise IdempotencyConflict("idempotency key already belongs to different text")
    return notification


def _credentials():
    try:
        token, chat_id = credential_values()
    except CredentialsFileError:
        raise ConfigurationError("Telegram credentials file is invalid or unsafe") from None
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token) or not re.fullmatch(
        r"(?:-?[0-9]+|@[A-Za-z0-9_]{5,})", chat_id
    ):
        raise ConfigurationError("Telegram credentials are missing or invalid")
    return token, chat_id


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _send(token, chat_id, text, timeout):
    """Return (outcome, safe error code, message id).

    Only explicit Telegram rejections are safe to retry. Network failures and
    malformed responses may occur after Telegram delivered the message.
    """
    request = Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=urlencode({"chat_id": chat_id, "text": text}).encode("utf-8"),
        method="POST",
    )
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            status = response.status
            payload = response.read(65537)
    except HTTPError as exc:
        status = exc.code
        try:
            payload = exc.read(65537)
        except (OSError, ValueError):
            return "uncertain", "response_unreadable", None
    except (URLError, OSError, TimeoutError, ValueError):
        return "uncertain", "transport_error", None
    try:
        if len(payload) > 65536:
            raise ValueError("response too large")
        body = json.loads(payload)
        if not isinstance(body, dict):
            raise ValueError("invalid response")
    except (ValueError, UnicodeDecodeError):
        return "uncertain", "invalid_response", None
    if status == 200 and body.get("ok") is True:
        result = body.get("result")
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if type(message_id) is int:
            return "sent", "", message_id
        return "uncertain", "invalid_response", None
    if body.get("ok") is False:
        if status == 429 or 500 <= status <= 599:
            return "retry", "telegram_rejected_retryable", None
        return "failed", "telegram_rejected", None
    return "uncertain", "invalid_response", None


def process_notifications(*, limit=100, max_attempts=3, retry_delay=300, timeout=10, lease_seconds=900):
    """Process due notifications and return a persisted JobRun.

    Expired claims are marked uncertain, never resent automatically. A crash
    after Telegram accepts a message cannot be distinguished from a failed send.
    """
    if limit < 1 or max_attempts < 1 or retry_delay < 1 or timeout <= 0 or lease_seconds <= 0:
        raise ValueError("processing options must be positive")
    run = JobRun.objects.create()
    try:
        token, chat_id = _credentials()
        now = timezone.now()
        run.uncertain = Notification.objects.filter(
            status=Notification.Status.SENDING,
            claimed_at__lt=now - timedelta(seconds=lease_seconds),
        ).update(
            status=Notification.Status.UNCERTAIN, last_error="expired_claim",
            claim_id=None, updated_at=now,
        )
        ids = list(Notification.objects.filter(
            status=Notification.Status.PENDING, next_attempt_at__lte=now,
        ).order_by("next_attempt_at", "pk").values_list("pk", flat=True)[:limit])
        for pk in ids:
            claim_id = uuid.uuid4()
            now = timezone.now()
            claimed = Notification.objects.filter(
                pk=pk, status=Notification.Status.PENDING, next_attempt_at__lte=now,
            ).update(
                status=Notification.Status.SENDING, claim_id=claim_id, claimed_at=now,
                attempts=F("attempts") + 1, updated_at=now,
            )
            if not claimed:
                continue
            notification = Notification.objects.get(pk=pk, claim_id=claim_id)
            run.attempted += 1
            outcome, error, message_id = _send(token, chat_id, notification.text, timeout)
            now = timezone.now()
            values = {"last_error": error, "claim_id": None, "updated_at": now}
            if outcome == "sent":
                values.update(status=Notification.Status.SENT, sent_at=now, telegram_message_id=message_id)
                count = "sent"
            elif outcome == "retry" and notification.attempts < max_attempts:
                values.update(status=Notification.Status.PENDING, next_attempt_at=now + timedelta(seconds=retry_delay))
                count = "retried"
            elif outcome == "uncertain":
                values["status"] = Notification.Status.UNCERTAIN
                count = "uncertain"
            else:
                values["status"] = Notification.Status.FAILED
                count = "failed"
            updated = Notification.objects.filter(
                pk=pk, status=Notification.Status.SENDING, claim_id=claim_id,
            ).update(**values)
            if updated:
                setattr(run, count, getattr(run, count) + 1)
            else:
                # Another worker marked the expired claim uncertain.
                run.uncertain += 1
        run.status = JobRun.Status.FAILED if run.failed or run.retried or run.uncertain else JobRun.Status.SUCCEEDED
    except ConfigurationError:
        run.status = JobRun.Status.FAILED
        run.error = "invalid_credentials"
        raise
    except Exception:
        run.status = JobRun.Status.FAILED
        run.error = "processing_error"
        raise ProcessingError("Notification processing failed; inspect JobRun.") from None
    finally:
        run.finished_at = timezone.now()
        run.save()
    return run
