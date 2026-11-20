from django.contrib import admin

from .models import JobRun, Notification


class ReadOnlyAdmin(admin.ModelAdmin):
    actions = None

    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in self.model._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Notification)
class NotificationAdmin(ReadOnlyAdmin):
    list_display = (
        "id", "idempotency_key", "status", "attempts", "next_attempt_at",
        "sent_at", "last_error", "created_at",
    )
    list_filter = ("status",)
    search_fields = ("idempotency_key",)
    ordering = ("-created_at",)


@admin.register(JobRun)
class JobRunAdmin(ReadOnlyAdmin):
    list_display = (
        "id", "status", "started_at", "finished_at", "attempted", "sent",
        "retried", "failed", "uncertain", "error",
    )
    list_filter = ("status",)
    ordering = ("-started_at",)
