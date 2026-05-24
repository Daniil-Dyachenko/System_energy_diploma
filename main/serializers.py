"""DRF serializers for the energy-monitoring API."""
from __future__ import annotations

from rest_framework import serializers

from .models import BalancingEvent, Device, DeviceEvent, SystemSettings, Telemetry


class DeviceSerializer(serializers.ModelSerializer):
    """Full CRUD representation of a Device (used by the web client / admin API)."""

    class Meta:
        model = Device
        fields = (
            'id',
            'name',
            'device_id',
            'description',
            'priority',
            'is_on',
            'last_power_watts',
            'last_seen_at',
            'shed_at',
            'created_at',
            'updated_at',
        )
        read_only_fields = (
            'id',
            'last_power_watts',
            'last_seen_at',
            'shed_at',
            'created_at',
            'updated_at',
        )


class DeviceStateSerializer(serializers.ModelSerializer):
    """Compact downlink payload for ESP32: which relay should be on/off."""

    class Meta:
        model = Device
        fields = ('device_id', 'is_on')


class TelemetryIngestSerializer(serializers.Serializer):
    """Uplink payload from the ESP32. Looks up the Device by its public device_id."""

    device_id = serializers.CharField(max_length=64)
    power_watts = serializers.FloatField(min_value=0.0)

    def validate_device_id(self, value: str) -> Device:
        try:
            device = Device.objects.get(device_id=value)
        except Device.DoesNotExist as exc:
            raise serializers.ValidationError(
                f'Unknown device_id "{value}" — register it in the admin first.'
            ) from exc
        return device

    def create(self, validated_data: dict) -> Telemetry:
        device: Device = validated_data['device_id']
        return Telemetry.objects.create(
            device=device,
            power_watts=validated_data['power_watts'],
        )


class TelemetryReadSerializer(serializers.ModelSerializer):
    """Read-only representation used by chart / history endpoints."""

    device_name = serializers.CharField(source='device.name', read_only=True)
    device_public_id = serializers.CharField(source='device.device_id', read_only=True)

    class Meta:
        model = Telemetry
        fields = (
            'id',
            'device',
            'device_name',
            'device_public_id',
            'power_watts',
            'timestamp',
        )
        read_only_fields = fields


class SystemSettingsSerializer(serializers.ModelSerializer):
    """Singleton system configuration."""

    class Meta:
        model = SystemSettings
        fields = (
            'power_limit_watts',
            'is_active',
            'restore_mode',
            'restore_cooldown_seconds',
            'updated_at',
        )
        read_only_fields = ('updated_at',)


class ChartDataPointSerializer(serializers.Serializer):
    """Aggregated bucket emitted by ChartDataView for Chart.js consumption."""

    timestamp = serializers.DateTimeField()
    total_power_watts = serializers.FloatField()


class CurrentLoadSerializer(serializers.Serializer):
    """Snapshot of the system: total load + system flags + per-device contribution."""

    total_power_watts = serializers.FloatField()
    power_limit_watts = serializers.IntegerField()
    is_overloaded = serializers.BooleanField()
    is_active = serializers.BooleanField()
    restore_mode = serializers.CharField()
    restore_cooldown_seconds = serializers.IntegerField()
    last_overload_at = serializers.DateTimeField(allow_null=True)
    devices = DeviceSerializer(many=True, read_only=True)


class DeviceEventSerializer(serializers.ModelSerializer):
    """Read-only audit row of a single relay state change (auto or manual)."""

    class Meta:
        model = DeviceEvent
        fields = ('id', 'action', 'power_watts', 'occurred_at')
        read_only_fields = fields


class DeviceHistoryMetricsSerializer(serializers.Serializer):
    """Aggregated numbers shown in the live card next to the chart."""

    average_watts = serializers.FloatField()
    peak_watts = serializers.FloatField()
    on_time_seconds = serializers.IntegerField()
    energy_kwh = serializers.FloatField()


class DeviceHistoryPointSerializer(serializers.Serializer):
    """One bucket of the per-device power chart."""

    timestamp = serializers.DateTimeField()
    power_watts = serializers.FloatField()


class DeviceHistorySerializer(serializers.Serializer):
    """One-shot payload for the device-detail page: live + chart + events."""

    device = DeviceSerializer()
    window = serializers.CharField()
    window_seconds = serializers.IntegerField()
    chart_data = DeviceHistoryPointSerializer(many=True)
    events = DeviceEventSerializer(many=True)
    metrics = DeviceHistoryMetricsSerializer()


class BalancingEventSerializer(serializers.ModelSerializer):
    """Read-only audit record of a single shed/restore action."""

    device_name = serializers.CharField(source='device.name', read_only=True)
    device_public_id = serializers.CharField(source='device.device_id', read_only=True)

    class Meta:
        model = BalancingEvent
        fields = (
            'id',
            'device',
            'device_name',
            'device_public_id',
            'action',
            'device_power_watts',
            'total_power_watts',
            'power_limit_watts',
            'occurred_at',
        )
        read_only_fields = fields