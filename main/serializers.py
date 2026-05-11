"""DRF serializers for the energy-monitoring API."""
from __future__ import annotations

from rest_framework import serializers

from .models import Device, SystemSettings, Telemetry


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
            'created_at',
            'updated_at',
        )
        read_only_fields = (
            'id',
            'last_power_watts',
            'last_seen_at',
            'created_at',
            'updated_at',
        )


class DeviceStateSerializer(serializers.ModelSerializer):

    class Meta:
        model = Device
        fields = ('device_id', 'is_on')


class TelemetryIngestSerializer(serializers.Serializer):

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

    class Meta:
        model = SystemSettings
        fields = ('power_limit_watts', 'is_active', 'updated_at')
        read_only_fields = ('updated_at',)


class ChartDataPointSerializer(serializers.Serializer):

    timestamp = serializers.DateTimeField()
    total_power_watts = serializers.FloatField()


class CurrentLoadSerializer(serializers.Serializer):

    total_power_watts = serializers.FloatField()
    power_limit_watts = serializers.IntegerField()
    is_overloaded = serializers.BooleanField()
    devices = DeviceStateSerializer(many=True)