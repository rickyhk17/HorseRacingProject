from django import forms
from django.contrib import admin, messages
from django.core.exceptions import ValidationError

from . import services
from .models import (
    ActualBet, ActualBetEvent, Meeting, Prediction, Publication, Race, RaceResult,
    Recommendation, Runner, SourcePost, VerificationEvent,
)


@admin.register(Meeting)
class MeetingAdmin(admin.ModelAdmin):
    list_display = ("date", "venue", "official_verified", "verified_by")


@admin.register(Race)
class RaceAdmin(admin.ModelAdmin):
    list_display = ("meeting", "number", "starts_at", "official_verified", "is_griffin")
    list_filter = ("meeting", "official_verified", "is_griffin")
    actions = ("show_candidate_ranking",)

    @admin.action(description="Rank selected eligible races (up to five)")
    def show_candidate_ranking(self, request, queryset):
        ranked = services.rank_candidates(queryset)
        if not ranked:
            self.message_user(request, "No selected races are eligible candidates.", messages.WARNING)
            return
        for position, item in enumerate(ranked, 1):
            scores = ", ".join(f"#{entry['runner'].number}: {entry['score']}"
                               for entry in item["runners"])
            self.message_user(request, f"{position}. {item['race']} — {item['score']} independent sources; "
                                       f"runner scores: {scores}", messages.INFO)


@admin.register(Runner)
class RunnerAdmin(admin.ModelAdmin):
    list_display = ("race", "number", "name", "active", "official_verified", "first_time")
    list_filter = ("race", "active", "official_verified")
    actions = ("approve_selected", "approve_late_revision")

    def _approve(self, request, queryset, late):
        runners = list(queryset.select_related("race").order_by("number"))
        if not runners or len({r.race_id for r in runners}) != 1:
            self.message_user(request, "Select four runners from the same race.", messages.ERROR)
            return
        try:
            prediction = services.approve_prediction(runners[0].race, runners, allow_late_revision=late)
        except ValidationError as exc:
            self.message_user(request, "; ".join(exc.messages), messages.ERROR)
        else:
            self.message_user(request, f"Approved {prediction}; publish separately.", messages.SUCCESS)

    @admin.action(description="Approve prediction using these four runners")
    def approve_selected(self, request, queryset):
        self._approve(request, queryset, False)

    @admin.action(description="Approve explicit late revision using these four runners")
    def approve_late_revision(self, request, queryset):
        self._approve(request, queryset, True)


@admin.register(SourcePost)
class SourcePostAdmin(admin.ModelAdmin):
    list_display = ("author", "source_type", "manually_verified", "verified_at", "verifier")
    list_filter = ("source_type", "manually_verified")
    readonly_fields = ("verified_at",)


@admin.register(Recommendation)
class RecommendationAdmin(admin.ModelAdmin):
    list_display = ("race", "runner", "post", "official_match_verified", "verifier", "verified_at")
    list_filter = ("race", "official_match_verified")
    readonly_fields = ("verified_at",)


class SnapshotAdmin(admin.ModelAdmin):
    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in self.model._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return obj is None and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        raise ValidationError("Snapshots are immutable.")

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions


@admin.register(Prediction)
class PredictionAdmin(SnapshotAdmin):
    list_display = ("race", "version", "approved_at", "published_at")
    actions = ("publish_selected",)

    @admin.display(description="Published at")
    def published_at(self, obj):
        return getattr(getattr(obj, "publication", None), "published_at", None)

    @admin.action(description="Publish selected approved snapshots")
    def publish_selected(self, request, queryset):
        for prediction in queryset:
            try:
                services.publish_prediction(prediction)
            except ValidationError as exc:
                self.message_user(request, f"{prediction}: {'; '.join(exc.messages)}", messages.ERROR)
            else:
                self.message_user(request, f"Published {prediction}.", messages.SUCCESS)


class ResultForm(forms.ModelForm):
    status = forms.ChoiceField(choices=[(s, s.title()) for s in ("pending", "final", "refund", "manual")])
    first = forms.IntegerField(required=False, min_value=1)
    second = forms.IntegerField(required=False, min_value=1)
    third = forms.IntegerField(required=False, min_value=1)
    dividend_base = forms.DecimalField(required=False, min_value=0)
    tierce_dividend = forms.DecimalField(required=False, min_value=0)
    trio_dividend = forms.DecimalField(required=False, min_value=0)
    official_verified = forms.BooleanField(required=False)
    actual_finished_at = forms.DateTimeField(required=False)
    notes = forms.CharField(required=False, widget=forms.Textarea)

    class Meta:
        model = RaceResult
        fields = ("race",)

    def result_kwargs(self):
        data = self.cleaned_data
        numbers = [data.get(key) for key in ("first", "second", "third")]
        return {
            "status": data.get("status"), "finishing_numbers": [n for n in numbers if n is not None],
            "dividend_base": data.get("dividend_base"), "tierce_dividend": data.get("tierce_dividend"),
            "trio_dividend": data.get("trio_dividend"), "official_verified": data.get("official_verified"),
            "actual_finished_at": data.get("actual_finished_at"), "notes": data.get("notes", ""),
        }

    def clean(self):
        data = super().clean()
        if data.get("race") and data.get("status") and not self.errors:
            try:
                services.validate_result(data["race"], **self.result_kwargs())
            except ValidationError as exc:
                self.add_error(None, exc)
        return data


@admin.register(RaceResult)
class RaceResultAdmin(SnapshotAdmin):
    form = ResultForm
    list_display = ("race", "version", "recorded_at", "actual_finished_at")

    def has_add_permission(self, request):
        return request.user.has_perm("racing.add_raceresult")

    def get_readonly_fields(self, request, obj=None):
        return super().get_readonly_fields(request, obj) if obj else ()

    def get_fields(self, request, obj=None):
        if obj:
            return ("race", "version", "recorded_at", "actual_finished_at", "inputs")
        return ("race", "status", "first", "second", "third", "dividend_base", "tierce_dividend",
                "trio_dividend", "official_verified", "actual_finished_at", "notes")

    def save_model(self, request, obj, form, change):
        if change:
            raise ValidationError("Results are immutable.")
        created = services.record_result(obj.race, **form.result_kwargs())
        obj.pk = created.pk
        obj.version = created.version
        obj.recorded_at = created.recorded_at
        obj.inputs = created.inputs
        obj.actual_finished_at = created.actual_finished_at
        obj._state.adding = False


@admin.register(ActualBet)
class ActualBetAdmin(admin.ModelAdmin):
    list_display = ("race", "placed_at", "stake", "payout", "refund_amount")

    def has_delete_permission(self, request, obj=None):
        return False


admin.site.register(Publication, SnapshotAdmin)
admin.site.register(VerificationEvent, SnapshotAdmin)
admin.site.register(ActualBetEvent, SnapshotAdmin)
