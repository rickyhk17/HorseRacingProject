import hashlib
import json
from datetime import timezone as datetime_timezone
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import models
from django.utils import timezone


MAX_PAYLOAD_BYTES = 64 * 1024


def canonical_payload(payload):
    """Validate structured JSON and return its deterministic UTF-8 encoding."""
    if not isinstance(payload, (dict, list)):
        raise ValidationError("Payload must be a JSON object or array.")

    def check_keys(value):
        if isinstance(value, dict):
            if any(not isinstance(key, str) for key in value):
                raise ValidationError("JSON object keys must be strings.")
            for item in value.values():
                check_keys(item)
        elif isinstance(value, list):
            for item in value:
                check_keys(item)
        elif value is not None and not isinstance(value, (str, int, float, bool)):
            raise ValidationError("Payload must contain only JSON values.")

    try:
        check_keys(payload)
        encoded = json.dumps(
            payload, sort_keys=True, ensure_ascii=False, allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError, UnicodeError) as exc:
        raise ValidationError("Payload must contain valid JSON.") from exc
    if len(encoded) > MAX_PAYLOAD_BYTES:
        raise ValidationError("Payload exceeds 64 KiB.")
    return encoded


class Observation(models.Model):
    class Status(models.TextChoices):
        SUCCESS = "success", "Success"
        FAILURE = "failure", "Failure"

    class Error(models.TextChoices):
        ACCESS_DENIED = "access_denied", "Access denied"
        NETWORK_ERROR = "network_error", "Network error"
        TIMEOUT = "timeout", "Timeout"
        HTTP_ERROR = "http_error", "HTTP error"
        INVALID_RESPONSE = "invalid_response", "Invalid response"
        PARSE_ERROR = "parse_error", "Parse error"
        TOO_LARGE = "too_large", "Response too large"
        NOT_AUTHORIZED = "not_authorized", "Not authorized"
        UNKNOWN_ERROR = "unknown_error", "Unknown error"

    source_url = models.URLField(max_length=2048, validators=[URLValidator(schemes=["http", "https"])])
    kind = models.CharField(max_length=64)
    retrieved_at = models.DateTimeField()
    status = models.CharField(max_length=7, choices=Status.choices)
    payload = models.JSONField(null=True, blank=True)
    error = models.CharField(max_length=32, choices=Error.choices, blank=True)
    fingerprint = models.CharField(max_length=64, unique=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-retrieved_at", "-pk"]

    def clean(self):
        super().clean()
        if self.source_url:
            try:
                parts = urlsplit(self.source_url)
                if parts.username is not None or parts.password is not None:
                    raise ValidationError({"source_url": "URLs with credentials are not allowed."})
                if parts.query or parts.fragment:
                    raise ValidationError({"source_url": "URLs must not contain a query or fragment."})
            except ValueError as exc:
                raise ValidationError({"source_url": "Enter a valid URL."}) from exc
        if not self.kind or not self.kind.strip() or self.kind != self.kind.strip():
            raise ValidationError({"kind": "Kind must be nonempty and have no surrounding whitespace."})
        if self.retrieved_at and timezone.is_naive(self.retrieved_at):
            raise ValidationError({"retrieved_at": "A timezone-aware timestamp is required."})
        if self.status == self.Status.SUCCESS:
            if self.error:
                raise ValidationError({"error": "Successful observations cannot have an error."})
            try:
                canonical_payload(self.payload)
            except ValidationError as exc:
                raise ValidationError({"payload": exc.messages}) from exc
        elif self.status == self.Status.FAILURE:
            if self.payload is not None:
                raise ValidationError({"payload": "Failures cannot have a payload."})
            if not self.error or not self.error.strip():
                raise ValidationError({"error": "Failures require an error description."})

    def calculate_fingerprint(self):
        content = canonical_payload(self.payload).decode("utf-8") if self.status == self.Status.SUCCESS else self.error
        identity = json.dumps(
            [self.source_url, self.kind, self.status, content,
             self.retrieved_at.astimezone(datetime_timezone.utc).isoformat()],
            ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(identity).hexdigest()

    def save(self, *args, **kwargs):
        self.full_clean(exclude=["fingerprint"], validate_unique=False, validate_constraints=False)
        self.fingerprint = self.calculate_fingerprint()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"Unverified {self.kind} ({self.status}) — {self.source_url}"
