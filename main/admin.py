from django.contrib import admin

from .models import Device, SystemSettings, Telemetry


@admin.register(Device)
class DeviceAdmin(admin.ModelAdmin):
    list_display = ('name', 'device_id', 'priority', 'is_on', 'updated_at')
    list_filter = ('is_on', 'priority')
    list_editable = ('priority', 'is_on')
    search_fields = ('name', 'device_id')
    ordering = ('priority', 'name')
    readonly_fields = ('created_at', 'updated_at')
    fieldsets = (
        (None, {'fields': ('name', 'device_id', 'description')}),
        ('Control', {'fields': ('priority', 'is_on')}),
        ('Timestamps', {'fields': ('created_at', 'updated_at')}),
    )


@admin.register(Telemetry)
class TelemetryAdmin(admin.ModelAdmin):
    list_display = ('device', 'power_watts', 'timestamp')
    list_filter = ('device',)
    search_fields = ('device__name', 'device__device_id')
    date_hierarchy = 'timestamp'
    readonly_fields = ('timestamp',)
    ordering = ('-timestamp',)


@admin.register(SystemSettings)
class SystemSettingsAdmin(admin.ModelAdmin):
    list_display = ('power_limit_watts', 'is_active', 'updated_at')
    readonly_fields = ('updated_at',)

    def has_add_permission(self, request):
        return not SystemSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False
