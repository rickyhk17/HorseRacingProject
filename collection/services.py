"""Local staging API. Captures are unverified and never alter racing data.

Callers must remove secrets (including secret URL path components and payload
values) before calling. URLs cannot contain credentials, query, or fragment.
Failure errors are fixed Observation.Error codes, not exception messages.
Identical content at a new retrieval time is a distinct observation.
"""

from .models import Observation


def _save(source_url, kind, retrieved_at, *, status, payload=None, error=""):
    candidate = Observation(
        source_url=source_url, kind=kind, retrieved_at=retrieved_at,
        status=status, payload=payload, error=error,
    )
    candidate.full_clean(exclude=["fingerprint"], validate_unique=False, validate_constraints=False)
    fingerprint = candidate.calculate_fingerprint()
    return Observation.objects.get_or_create(
        fingerprint=fingerprint,
        defaults={
            "source_url": source_url, "kind": kind, "retrieved_at": retrieved_at,
            "status": status, "payload": payload, "error": error,
        },
    )


def save_observation(source_url, kind, payload, retrieved_at):
    """Stage an unverified capture; return (observation, created)."""
    return _save(source_url, kind, retrieved_at, status=Observation.Status.SUCCESS, payload=payload)


def record_failure(source_url, kind, error, retrieved_at):
    """Stage a bounded error; return (observation, created)."""
    return _save(source_url, kind, retrieved_at, status=Observation.Status.FAILURE, error=error)
