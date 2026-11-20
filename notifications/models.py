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
