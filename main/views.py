"""DRF views exposing the REST API for ESP32 devices and the web client."""
from __future__ import annotations

from datetime import timedelta

from django.db.models import Sum
from django.db.models.functions import TruncMinute
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Device, SystemSettings, Telemetry
from .permissions import HasDeviceApiKey
from .serializers import (
    ChartDataPointSerializer,
    CurrentLoadSerializer,
    DeviceSerializer,
    DeviceStateSerializer,
    SystemSettingsSerializer,
    TelemetryIngestSerializer,
    TelemetryReadSerializer,
)
from .services import ingest_and_rebalance

class TelemetryIngestView(APIView):
    """POST /api/telemetry/ — ESP32 reports a power-draw sample.
    """

    permission_classes = [HasDeviceApiKey]

    def post(self, request):
        serializer = TelemetryIngestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        device: Device = serializer.validated_data['device_id']
        power_watts: float = serializer.validated_data['power_watts']

        sample, report = ingest_and_rebalance(device, power_watts)

        return Response(
            {
                'telemetry': TelemetryReadSerializer(sample).data,
                'balancing': {
                    'total_power_watts': report.total_power_watts,
                    'power_limit_watts': report.power_limit_watts,
                    'is_overloaded': report.is_overloaded,
                    'shed_devices': report.shed_devices,
                    'restored_devices': report.restored_devices,
                },
            },
            status=status.HTTP_201_CREATED,
        )


class DeviceStateView(APIView):
    """GET /api/device-state/ — ESP32 polls relay states.
    """

    permission_classes = [HasDeviceApiKey]

    def get(self, request):
        device_id = request.query_params.get('device_id')
        if device_id:
            try:
                device = Device.objects.get(device_id=device_id)
            except Device.DoesNotExist:
                return Response(
                    {'detail': f'Device "{device_id}" not found.'},
                    status=status.HTTP_404_NOT_FOUND,
                )
            return Response(DeviceStateSerializer(device).data)

        devices = Device.objects.all()
        return Response(DeviceStateSerializer(devices, many=True).data)

class ChartDataView(APIView):
    """GET /api/chart-data/ — total system load aggregated by minute.
    """

    permission_classes = [AllowAny]
    DEFAULT_WINDOW_MINUTES = 30
    MAX_WINDOW_MINUTES = 24 * 60

    def get(self, request):
        try:
            window = int(request.query_params.get('minutes', self.DEFAULT_WINDOW_MINUTES))
        except (TypeError, ValueError):
            window = self.DEFAULT_WINDOW_MINUTES
        window = max(1, min(window, self.MAX_WINDOW_MINUTES))

        since = timezone.now() - timedelta(minutes=window)
        rows = (
            Telemetry.objects.filter(timestamp__gte=since)
            .annotate(bucket=TruncMinute('timestamp'))
            .values('bucket')
            .annotate(total_power_watts=Sum('power_watts'))
            .order_by('bucket')
        )
        data = [
            {'timestamp': row['bucket'], 'total_power_watts': row['total_power_watts'] or 0.0}
            for row in rows
        ]
        return Response(ChartDataPointSerializer(data, many=True).data)


class SystemSettingsView(APIView):
    """GET/POST /api/settings/ — read or update the singleton system settings."""

    permission_classes = [AllowAny]  # Не забути в майбутньому підкоректувати

    def get(self, request):
        return Response(SystemSettingsSerializer(SystemSettings.load()).data)

    def post(self, request):
        instance = SystemSettings.load()
        serializer = SystemSettingsSerializer(instance, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class CurrentLoadView(APIView):
    """GET /api/current-load/ — instant snapshot for the dashboard widget."""

    permission_classes = [AllowAny]

    def get(self, request):
        devices = list(Device.objects.all())
        total = sum(d.last_power_watts for d in devices if d.is_on)
        limit = SystemSettings.load().power_limit_watts
        payload = {
            'total_power_watts': total,
            'power_limit_watts': limit,
            'is_overloaded': total > limit,
            'devices': devices,
        }
        return Response(CurrentLoadSerializer(payload).data)


class DeviceViewSet(viewsets.ModelViewSet):
    """CRUD for devices — used by the admin web UI and management scripts."""

    queryset = Device.objects.all()
    serializer_class = DeviceSerializer
    permission_classes = [AllowAny]  # Так само, не забути підкоректувати

    @action(detail=True, methods=['post'], url_path='toggle')
    def toggle(self, request, pk=None):
        device = self.get_object()
        device.is_on = not device.is_on
        device.save(update_fields=['is_on', 'updated_at'])
        return Response(DeviceSerializer(device).data)