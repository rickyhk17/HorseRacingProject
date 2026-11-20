from django.contrib import admin

from .models import Observation


@admin.register(Observation)
class ObservationAdmin(admin.ModelAdmin):
    list_display = ("id", "kind", "status", "source_url", "retrieved_at", "verification")
    list_filter = ("kind", "status")
    search_fields = ("source_url", "kind", "error")
    readonly_fields = (
        "source_url", "kind", "retrieved_at", "status", "payload", "error",
        "fingerprint", "created_at", "verification",
    )
    fields = readonly_fields
    actions = None

    @admin.display(description="Verification")
    def verification(self, obj):
        return "Unverified — copy into the appropriate racing form for manual review"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
