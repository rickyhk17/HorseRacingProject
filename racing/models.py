from urllib.parse import urlsplit, urlunsplit

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models, transaction
from django.utils import timezone


class ValidatedModel(models.Model):
    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        tracked = self.__class__.__name__ in {"Meeting", "Race", "Runner"}
        old = type(self).objects.filter(pk=self.pk).first() if tracked and self.pk else None
        self.full_clean()
        with transaction.atomic():
            result = super().save(*args, **kwargs)
            if tracked and (old is None or old.official_verified != self.official_verified):
                VerificationEvent.objects.create(subject=self.__class__.__name__.lower(), subject_id=self.pk,
                                                 verified=self.official_verified, operator=self.verified_by)
            if self.__class__.__name__ == "Runner" and (old is None or old.active != self.active):
                VerificationEvent.objects.create(subject="runner-active", subject_id=self.pk,
                                                 verified=self.active, operator=self.verified_by)
            return result


class Meeting(ValidatedModel):
    date = models.DateField()
    venue = models.CharField(max_length=100)
    country = models.CharField(max_length=2, default="HK", editable=False)
    official_verified = models.BooleanField(default=False)
    verified_by = models.CharField(max_length=150, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["date", "venue"], name="unique_meeting"),
            models.CheckConstraint(condition=models.Q(country="HK"), name="meeting_hk_only"),
        ]

    def __str__(self):
        return f"{self.date} {self.venue}"


class Race(ValidatedModel):
    meeting = models.ForeignKey(Meeting, on_delete=models.PROTECT, related_name="races")
    number = models.PositiveSmallIntegerField(validators=[MinValueValidator(1)])
    name = models.CharField(max_length=150)
    starts_at = models.DateTimeField()
    official_verified = models.BooleanField(default=False)
    verified_by = models.CharField(max_length=150, blank=True)
    is_griffin = models.BooleanField(default=False)
    approval_rationale = models.TextField(blank=True, help_text="Explain the race choice and all four selected runners.")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["meeting", "number"], name="unique_race_number")]

    def __str__(self):
        return f"{self.meeting} R{self.number}"


class Runner(ValidatedModel):
    race = models.ForeignKey(Race, on_delete=models.PROTECT, related_name="runners")
    number = models.PositiveSmallIntegerField(validators=[MinValueValidator(1)])
    name = models.CharField(max_length=150)
    active = models.BooleanField(default=True)
    official_verified = models.BooleanField(default=False)
    verified_by = models.CharField(max_length=150, blank=True)
    first_time = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["race", "number"], name="unique_runner_number")]

    def __str__(self):
        return f"{self.race} #{self.number} {self.name}"


def canonical_threads_url(value):
    parts = urlsplit(value.strip())
    host = (parts.hostname or "").lower()
    path = parts.path.rstrip("/")
    segments = path.split("/")
    valid_path = (
        len(segments) == 4 and segments[1].startswith("@")
        and len(segments[1]) > 1 and segments[2] == "post" and bool(segments[3])
    ) or (len(segments) == 3 and segments[1] == "t" and bool(segments[2]))
    if (parts.scheme.lower() != "https" or host not in {
        "threads.net", "www.threads.net", "threads.com", "www.threads.com"
    } or parts.username or parts.password or parts.port or not valid_path):
        raise ValidationError("Enter a public HTTPS Threads post URL.")
    if segments[1].startswith("@"):
        segments[1] = segments[1].lower()
    return urlunsplit(("https", "www.threads.net", "/".join(segments), "", ""))


class SourcePost(ValidatedModel):
    class SourceType(models.TextChoices):
        THREADS = "threads", "Threads"
        ONCC = "oncc", "on.cc"
        HORSERACINGHK = "horseracinghk", "Horse Racing HK"
        OTHER = "other", "Other"

    source_type = models.CharField(max_length=20, choices=SourceType.choices, default=SourceType.THREADS)
    url = models.URLField(unique=True)
    author = models.CharField(max_length=150)
    summary = models.TextField(blank=True)
    followers = models.PositiveIntegerField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    retrieved_at = models.DateTimeField(null=True, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True, editable=False)
    manually_verified = models.BooleanField(default=False)
    verifier = models.CharField(max_length=150, blank=True)
    # Posts sharing this key count as one independent source in each race.
    syndication_key = models.CharField(max_length=150, blank=True)

    def clean(self):
        super().clean()
        old = type(self).objects.filter(pk=self.pk).first() if self.pk else None
        if old and old.manually_verified and self.manually_verified:
            protected = ("source_type", "url", "author", "summary", "followers", "published_at",
                         "retrieved_at", "verifier", "syndication_key")
            if any(getattr(old, name) != getattr(self, name) for name in protected):
                raise ValidationError("Unverify the source before changing verified information.")
        if self.url and (urlsplit(self.url).hostname or "").lower() in {
            "threads.net", "www.threads.net", "threads.com", "www.threads.com"
        } and self.source_type != self.SourceType.THREADS:
            raise ValidationError({"source_type": "Threads links must use the Threads source type."})
        if self.url and self.source_type == self.SourceType.THREADS:
            try:
                self.url = canonical_threads_url(self.url)
            except ValueError:
                raise ValidationError({"url": "Enter a valid Threads post URL."})
        elif self.url:
            parts = urlsplit(self.url.strip())
            if parts.scheme.lower() != "https" or not parts.hostname or parts.username or parts.password:
                raise ValidationError({"url": "Enter a public HTTPS source URL."})
            self.url = urlunsplit(("https", parts.netloc.lower(), parts.path.rstrip("/"), "", ""))
        if self.manually_verified and self.source_type == self.SourceType.THREADS and (
            self.followers is None or self.followers <= 1000
        ):
            raise ValidationError({"followers": "The author must have more than 1,000 followers."})
        if self.manually_verified and not all((self.author, self.summary, self.verifier,
                                               self.published_at, self.retrieved_at)):
            raise ValidationError("Verified sources require author, summary, verifier, publication and retrieval times.")
        if self.manually_verified and not self.verified_at:
            self.verified_at = timezone.now()
        if self.published_at and self.retrieved_at and self.verified_at:
            if not self.published_at <= self.retrieved_at <= self.verified_at:
                raise ValidationError("Publication, retrieval and verification must be in chronological order.")
            if self.verified_at > timezone.now():
                raise ValidationError({"verified_at": "Verification cannot be in the future."})
        self.syndication_key = self.syndication_key.strip().casefold()

    def __str__(self):
        return f"{self.author}: {self.url}"

    @transaction.atomic
    def save(self, *args, **kwargs):
        old = type(self).objects.filter(pk=self.pk).first() if self.pk else None
        if old and old.manually_verified and self.manually_verified:
            self.verified_at = old.verified_at
        elif self.manually_verified:
            self.verified_at = timezone.now()
        else:
            self.verified_at = None
        super().save(*args, **kwargs)
        if old is None or old.manually_verified != self.manually_verified:
            VerificationEvent.objects.create(subject="source", subject_id=self.pk,
                                             verified=self.manually_verified, operator=self.verifier)


class Recommendation(ValidatedModel):
    post = models.ForeignKey(SourcePost, on_delete=models.PROTECT, related_name="recommendations")
    race = models.ForeignKey(Race, on_delete=models.PROTECT, related_name="recommendations")
    runner = models.ForeignKey(Runner, on_delete=models.PROTECT, related_name="recommendations")
    official_match_verified = models.BooleanField(default=False)
    verifier = models.CharField(max_length=150, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["post", "race", "runner"], name="unique_recommendation")]

    def clean(self):
        super().clean()
        old = type(self).objects.filter(pk=self.pk).first() if self.pk else None
        if old and old.official_match_verified and self.official_match_verified and any(
            getattr(old, name) != getattr(self, name) for name in ("post_id", "race_id", "runner_id", "verifier")
        ):
            raise ValidationError("Unverify the match before changing verified information.")
        if self.runner_id and self.race_id and self.runner.race_id != self.race_id:
            raise ValidationError({"runner": "Runner must belong to this race."})
        if self.official_match_verified and not self.verifier.strip():
            raise ValidationError({"verifier": "Record who verified the official match."})

    @transaction.atomic
    def save(self, *args, **kwargs):
        old = type(self).objects.filter(pk=self.pk).first() if self.pk else None
        if old and old.official_match_verified and self.official_match_verified:
            self.verified_at = old.verified_at
        elif self.official_match_verified:
            self.verified_at = timezone.now()
        else:
            self.verified_at = None
        super().save(*args, **kwargs)
        if old is None or old.official_match_verified != self.official_match_verified:
            VerificationEvent.objects.create(subject="match", subject_id=self.pk,
                                             verified=self.official_match_verified, operator=self.verifier)

    def __str__(self):
        return f"{self.post.author}: {self.runner}"


class ImmutableSnapshot(models.Model):
    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Published snapshots cannot be changed.")
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Published snapshots cannot be deleted.")


class Prediction(ImmutableSnapshot):
    race = models.ForeignKey(Race, on_delete=models.PROTECT, related_name="predictions")
    version = models.PositiveIntegerField()
    approved_at = models.DateTimeField()
    inputs = models.JSONField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["race", "version"], name="unique_prediction_version")]
        ordering = ["race_id", "-version"]

    def __str__(self):
        return f"{self.race} prediction v{self.version}"


class RaceResult(ImmutableSnapshot):
    race = models.ForeignKey(Race, on_delete=models.PROTECT, related_name="results")
    version = models.PositiveIntegerField()
    recorded_at = models.DateTimeField()
    actual_finished_at = models.DateTimeField(null=True, blank=True)
    inputs = models.JSONField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["race", "version"], name="unique_result_version")]
        ordering = ["race_id", "-version"]

    def __str__(self):
        return f"{self.race} result v{self.version}"


class Publication(ImmutableSnapshot):
    prediction = models.OneToOneField(Prediction, on_delete=models.PROTECT, related_name="publication")
    published_at = models.DateTimeField()

    def __str__(self):
        return f"Publication of {self.prediction}"


class VerificationEvent(ImmutableSnapshot):
    subject = models.CharField(max_length=20)
    subject_id = models.PositiveBigIntegerField()
    verified = models.BooleanField()
    operator = models.CharField(max_length=150, blank=True)
    recorded_at = models.DateTimeField(default=timezone.now)


class ActualBet(ValidatedModel):
    race = models.ForeignKey(Race, on_delete=models.PROTECT, related_name="actual_bets")
    placed_at = models.DateTimeField()
    description = models.TextField()
    stake = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(0.01)])
    payout = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True,
                                 validators=[MinValueValidator(0)])
    refund_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True,
                                        validators=[MinValueValidator(0)])
    notes = models.TextField(blank=True)

    def __str__(self):
        return f"{self.race}: {self.description}"

    def save(self, *args, **kwargs):
        with transaction.atomic():
            super().save(*args, **kwargs)
            ActualBetEvent.objects.create(bet=self, inputs={
                "race_id": self.race_id, "placed_at": self.placed_at.isoformat(),
                "description": self.description, "stake": str(self.stake),
                "payout": str(self.payout) if self.payout is not None else None,
                "refund_amount": str(self.refund_amount) if self.refund_amount is not None else None,
                "notes": self.notes,
            })


class ActualBetEvent(ImmutableSnapshot):
    bet = models.ForeignKey(ActualBet, on_delete=models.PROTECT, related_name="events")
    recorded_at = models.DateTimeField(default=timezone.now)
    inputs = models.JSONField()
