from django.db import models
from django.utils import timezone


class Notification(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SENDING = "sending", "Sending"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"
        UNCERTAIN = "uncertain", "Delivery uncertain"

    idempotency_key = models.CharField(max_length=255, unique=True)
    text = models.TextField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    claimed_at = models.DateTimeField(null=True, blank=True)
    claim_id = models.UUIDField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    telegram_message_id = models.BigIntegerField(null=True, blank=True)
    last_error = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["status", "next_attempt_at"])]


class JobRun(models.Model):
    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.RUNNING)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    attempted = models.PositiveIntegerField(default=0)
    sent = models.PositiveIntegerField(default=0)
    retried = models.PositiveIntegerField(default=0)
    failed = models.PositiveIntegerField(default=0)
    uncertain = models.PositiveIntegerField(default=0)
    error = models.CharField(max_length=64, blank=True)


class TelegramPollState(models.Model):
    # Singleton row: only one configured bot is supported by the outbox.
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    next_offset = models.BigIntegerField(default=0)
    chat_id = models.BigIntegerField()
    user_id = models.BigIntegerField()
    last_update_at = models.DateTimeField(null=True, blank=True)
    bot_id = models.BigIntegerField(null=True, blank=True)


class TelegramPollLock(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    acquired_at = models.DateTimeField(auto_now_add=True)


class TelegramCommand(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        UNCERTAIN = "uncertain", "Execution uncertain"

    update_id = models.BigIntegerField(unique=True)
    text = models.TextField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
