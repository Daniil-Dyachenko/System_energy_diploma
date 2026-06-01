"""DRF views exposing the REST API for ESP32 devices and the web client."""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from django.db.models import Avg, Max, Min
from django.db.models.functions import TruncHour, TruncMinute
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from .analytics import compute_consumption_summary, resolve_period
from .forecasting import compute_forecast, resolve_forecast_params
from .models import BalancingEvent, Device, DeviceEvent, SystemSettings, Telemetry
from .permissions import HasDeviceApiKey
from .serializers import (
    BalancingEventSerializer,
    ChartDataPointSerializer,
    ConsumptionSummarySerializer,
    CurrentLoadSerializer,
    DeviceHistorySerializer,
    DeviceSerializer,
    DeviceStateSerializer,
    ForecastSerializer,
    SystemSettingsSerializer,
    TelemetryIngestSerializer,
    TelemetryReadSerializer,
)
from .services import ingest_and_rebalance


# ESP32-facing endpoints (X-API-Key required)

class TelemetryIngestView(APIView):
    """POST /api/telemetry/ — ESP32 reports a power-draw sample."""

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
                    'restore_mode': report.restore_mode,
                    'shed_devices': report.shed_devices,
                    'restored_devices': report.restored_devices,
                },
            },
            status=status.HTTP_201_CREATED,
        )


class DeviceStateView(APIView):
    """GET /api/device-state/ — ESP32 polls relay states."""

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


# Web-client endpoints (currently open for development; auth comes in stage 5)

class ChartDataView(APIView):
    """GET /api/chart-data/ — total system load aggregated by minute."""

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

        per_device = (
            Telemetry.objects.filter(timestamp__gte=since, is_on=True)
            .annotate(bucket=TruncMinute('timestamp'))
            .values('bucket', 'device_id')
            .annotate(avg_power=Avg('power_watts'))
            .order_by('bucket')
        )

        buckets: dict = defaultdict(float)
        for row in per_device:
            buckets[row['bucket']] += float(row['avg_power'] or 0.0)

        data = [
            {'timestamp': bucket, 'total_power_watts': total}
            for bucket, total in sorted(buckets.items())
        ]
        return Response(ChartDataPointSerializer(data, many=True).data)



class SystemSettingsView(APIView):
    """GET/POST /api/settings/ — read or update the singleton system settings."""

    permission_classes = [AllowAny]  # tightened in stage 5

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
        settings_row = SystemSettings.load()
        last_shed = (
            BalancingEvent.objects
            .filter(action=BalancingEvent.Action.SHED)
            .order_by('-occurred_at')
            .values_list('occurred_at', flat=True)
            .first()
        )
        payload = {
            'total_power_watts': total,
            'power_limit_watts': settings_row.power_limit_watts,
            'is_overloaded': total > settings_row.power_limit_watts,
            'is_active': settings_row.is_active,
            'restore_mode': settings_row.restore_mode,
            'restore_cooldown_seconds': settings_row.restore_cooldown_seconds,
            'last_overload_at': last_shed,
            'devices': devices,
        }
        return Response(CurrentLoadSerializer(payload).data)



class AccountSummaryView(APIView):
    """GET /api/account/summary/ — Cabinet page totals."""

    permission_classes = [AllowAny]  # tightened in stage 5

    def get(self, request):
        try:
            since, until = resolve_period(
                request.query_params.get('since'),
                request.query_params.get('until'),
            )
        except ValueError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        summary = compute_consumption_summary(since, until)
        return Response(ConsumptionSummarySerializer(summary).data)


class ForecastView(APIView):
    """GET /api/forecast/ - short-term forecast of total system consumption."""

    permission_classes = [AllowAny]  # tightened in stage 5

    def get(self, request):
        try:
            params = resolve_forecast_params(request.query_params)
        except ValueError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        result = compute_forecast(**params)
        return Response(ForecastSerializer(result).data)

class BalancingEventsView(APIView):
    """GET /api/balancing-events/?limit=N — most recent shed/restore actions."""

    permission_classes = [AllowAny]  # tightened in stage 5
    DEFAULT_LIMIT = 20
    MAX_LIMIT = 200

    def get(self, request):
        try:
            limit = int(request.query_params.get('limit', self.DEFAULT_LIMIT))
        except (TypeError, ValueError):
            limit = self.DEFAULT_LIMIT
        limit = max(1, min(limit, self.MAX_LIMIT))

        events = (
            BalancingEvent.objects
            .select_related('device')
            .order_by('-occurred_at')[:limit]
        )
        return Response(BalancingEventSerializer(events, many=True).data)


class DeviceViewSet(viewsets.ModelViewSet):
    """CRUD for devices — used by the admin web UI and management scripts."""

    queryset = Device.objects.all()
    serializer_class = DeviceSerializer
    permission_classes = [AllowAny]  # tightened in stage 5

    @action(detail=True, methods=['get'], url_path='history', permission_classes=[AllowAny])
    def history(self, request, pk=None):
        """One-shot payload for the device-detail page."""
        device: Device = self.get_object()
        window_key = request.query_params.get('window', '1h')
        window_minutes, granularity = _DEVICE_HISTORY_WINDOWS.get(
            window_key, _DEVICE_HISTORY_WINDOWS['1h'],
        )
        now = timezone.now()

        sample_bounds = (
            Telemetry.objects
            .filter(device=device, is_on=True)
            .aggregate(first=Min('timestamp'), last=Max('timestamp'))
        )
        first_sample = sample_bounds['first']
        last_sample = sample_bounds['last']

        if last_sample is None:
            chart_data: list[dict] = []
        else:
            anchor_until = last_sample
            anchor_since = max(
                anchor_until - timedelta(minutes=window_minutes),
                first_sample,
            )
            chart_data = _build_device_chart(
                device, anchor_since, anchor_until, granularity,
            )

        events = list(
            DeviceEvent.objects
            .filter(device=device)
            .order_by('-occurred_at')[:50]
        )
        metrics = _compute_device_metrics(
            device, now, window_minutes,
            first_sample=first_sample, last_sample=last_sample,
        )

        payload = {
            'device': device,
            'window': window_key,
            'window_seconds': window_minutes * 60,
            'chart_data': chart_data,
            'events': events,
            'metrics': metrics,
        }
        return Response(DeviceHistorySerializer(payload).data)

    @action(detail=True, methods=['post'], url_path='toggle')
    def toggle(self, request, pk=None):
        """Manual relay toggle from the web UI (bypasses the balancing alg)."""
        device = self.get_object()
        device.is_on = not device.is_on
        update_fields = ['is_on', 'updated_at']
        if device.is_on:
            device.shed_at = None
            update_fields.append('shed_at')
        device.save(update_fields=update_fields)
        DeviceEvent.objects.create(
            device=device,
            action=(
                DeviceEvent.Action.MANUAL_ON if device.is_on
                else DeviceEvent.Action.MANUAL_OFF
            ),
            power_watts=device.last_power_watts,
        )
        return Response(DeviceSerializer(device).data)

_DEVICE_HISTORY_WINDOWS = {
    '5m': (5, 'minute'),
    '1h': (60, 'minute'),
    '1d': (24 * 60, 'hour'),
    '7d': (7 * 24 * 60, 'hour'),
}

_ON_ACTIONS = frozenset((DeviceEvent.Action.RESTORE, DeviceEvent.Action.MANUAL_ON))


def _build_device_chart(device: Device, since, until, granularity: str) -> list[dict]:
    """Aggregate per-device telemetry into (timestamp, power_watts) buckets."""

    trunc = TruncMinute('timestamp') if granularity == 'minute' else TruncHour('timestamp')
    rows = (
        Telemetry.objects
        .filter(
            device=device,
            timestamp__gte=since,
            timestamp__lte=until,
            is_on=True,
        )
        .annotate(bucket=trunc)
        .values('bucket')
        .annotate(avg_power=Avg('power_watts'))
        .order_by('bucket')
    )
    return [
        {'timestamp': r['bucket'], 'power_watts': float(r['avg_power'] or 0.0)}
        for r in rows
    ]


def _compute_device_metrics(
    device: Device,
    now,
    window_minutes: int,
    first_sample,
    last_sample,
) -> dict:
    """Compute avg / peak / on-time / energy."""

    if last_sample is None:
        avg_watts = 0.0
        peak_watts = 0.0
        energy_kwh = 0.0
    else:
        chart_until = last_sample
        chart_since = max(
            chart_until - timedelta(minutes=window_minutes),
            first_sample,
        )
        agg = (
            Telemetry.objects
            .filter(
                device=device,
                timestamp__gte=chart_since,
                timestamp__lte=chart_until,
                is_on=True,
            )
            .aggregate(avg=Avg('power_watts'), peak=Max('power_watts'))
        )
        avg_watts = float(agg['avg'] or 0.0)
        peak_watts = float(agg['peak'] or 0.0)
        covered_hours = max(0.0, (chart_until - chart_since).total_seconds() / 3600.0)
        energy_kwh = (avg_watts * covered_hours) / 1000.0

    on_seconds = _current_on_session_seconds(device, now)

    return {
        'average_watts': round(avg_watts, 2),
        'peak_watts': round(peak_watts, 2),
        'on_time_seconds': int(on_seconds),
        'energy_kwh': round(energy_kwh, 4),
    }


def _current_on_session_seconds(device: Device, now) -> float:
    """Return seconds since the device most recently entered the ON state."""

    if not device.is_on:
        return 0.0

    last_on_event = (
        DeviceEvent.objects
        .filter(device=device, action__in=tuple(_ON_ACTIONS))
        .order_by('-occurred_at')
        .first()
    )
    started_at = last_on_event.occurred_at if last_on_event else device.created_at
    return max(0.0, (now - started_at).total_seconds())