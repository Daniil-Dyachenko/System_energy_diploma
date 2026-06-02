from django.contrib import admin

from .models import BalancingEvent, Device, DeviceEvent, SystemSettings, Telemetry, generate_device_api_key


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
    actions = ('regenerate_api_keys',)
    readonly_fields = (
        'last_power_watts',
        'last_seen_at',
        'shed_at',
        'created_at',
        'updated_at',
    )
    fieldsets = (
        (None, {'fields': ('name', 'device_id', 'description')}),
        ('Security / provisioning', {
            'fields': ('api_key',),
            'description': (
                'Per-device X-API-Key the ESP32 must present. The add form is '
                'pre-filled with a fresh value — copy it into that board\'s '
                'firmware secrets.h. Use the "Regenerate API key" action to '
                'rotate a compromised key (and re-flash that one board).'
            ),
        }),
        ('Control', {'fields': ('priority', 'is_on')}),
        ('Live state', {'fields': ('last_power_watts', 'last_seen_at', 'shed_at')}),
        ('Timestamps', {'fields': ('created_at', 'updated_at')}),
    )


    def get_changeform_initial_data(self, request):
        initial = super().get_changeform_initial_data(request)
        initial.setdefault('api_key', generate_device_api_key())
        return initial

    @admin.action(description='Regenerate API key for selected devices')
    def regenerate_api_keys(self, request, queryset):
        count = 0
        for device in queryset:
            device.api_key = generate_device_api_key()
            device.save(update_fields=['api_key', 'updated_at'])
            count += 1
        self.message_user(request, f'Regenerated API key for {count} device(s).')



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
