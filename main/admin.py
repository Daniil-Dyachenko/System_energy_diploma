from django.contrib import admin

from .models import BalancingEvent, Device, DeviceEvent, SystemSettings, Telemetry


@admin.register(Device)
class DeviceAdmin(admin.ModelAdmin):
    list_display = (
        'name',
        'device_id',
        'priority',
        'is_on',
        'last_power_watts',
        'last_seen_at',
        'shed_at',
        'updated_at',
    )
    list_filter = ('is_on', 'priority')
    list_editable = ('priority', 'is_on')
    search_fields = ('name', 'device_id')
    ordering = ('priority', 'name')
    readonly_fields = (
        'last_power_watts',
        'last_seen_at',
        'shed_at',
        'created_at',
        'updated_at',
    )
    fieldsets = (
        (None, {'fields': ('name', 'device_id', 'description')}),
        ('Control', {'fields': ('priority', 'is_on')}),
        ('Live state', {'fields': ('last_power_watts', 'last_seen_at', 'shed_at')}),
        ('Timestamps', {'fields': ('created_at', 'updated_at')}),
    )


@admin.register(Telemetry)
class TelemetryAdmin(admin.ModelAdmin):
    list_display = ('device', 'power_watts', 'is_on', 'timestamp')
    list_filter = ('is_on', 'device')
    search_fields = ('device__name', 'device__device_id')
    date_hierarchy = 'timestamp'
    readonly_fields = ('timestamp',)
    ordering = ('-timestamp',)


@admin.register(DeviceEvent)
class DeviceEventAdmin(admin.ModelAdmin):
    list_display = ('occurred_at', 'action', 'device', 'power_watts')
    list_filter = ('action', 'device')
    search_fields = ('device__name', 'device__device_id')
    date_hierarchy = 'occurred_at'
    readonly_fields = ('device', 'action', 'power_watts', 'occurred_at')
    ordering = ('-occurred_at',)

    def has_add_permission(self, request):
        return False


@admin.register(BalancingEvent)
class BalancingEventAdmin(admin.ModelAdmin):
    list_display = (
        'occurred_at',
        'action',
        'device',
        'device_power_watts',
        'total_power_watts',
        'power_limit_watts',
    )
    list_filter = ('action', 'device')
    search_fields = ('device__name', 'device__device_id')
    date_hierarchy = 'occurred_at'
    readonly_fields = (
        'device',
        'action',
        'device_power_watts',
        'total_power_watts',
        'power_limit_watts',
        'occurred_at',
    )
    ordering = ('-occurred_at',)

    def has_add_permission(self, request):
        return False


@admin.register(SystemSettings)
class SystemSettingsAdmin(admin.ModelAdmin):
    list_display = (
        'power_limit_watts',
        'is_active',
        'restore_mode',
        'restore_cooldown_seconds',
        'tariff_uah_per_kwh',
        'updated_at',
    )
    readonly_fields = ('updated_at',)

    def has_add_permission(self, request):
        return not SystemSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False
