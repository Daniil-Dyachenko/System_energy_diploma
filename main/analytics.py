"""Cabinet-page analytics: per-device and total kWh + UAH cost."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Iterable

from django.db.models import Avg
from django.db.models.functions import TruncMinute
from django.utils import timezone

from .models import Device, SystemSettings, Telemetry


@dataclass
class DeviceConsumption:
    """Per-device row shown in the Cabinet table."""

    device: Device
    period_kwh: float = 0.0
    lifetime_kwh: float = 0.0
    period_uah: float = 0.0
    lifetime_uah: float = 0.0


@dataclass
class ConsumptionSummary:
    """Full payload returned."""

    since: datetime
    until: datetime
    tariff_uah_per_kwh: Decimal
    period_kwh: float = 0.0
    lifetime_kwh: float = 0.0
    period_uah: float = 0.0
    lifetime_uah: float = 0.0
    devices: list[DeviceConsumption] = field(default_factory=list)

    @property
    def days(self) -> int:
        """Number of whole days."""
        return max(1, (self.until.date() - self.since.date()).days + 1)


def _kwh_for_device(device: Device, since: datetime | None, until: datetime | None) -> float:
    """Energy consumed by ``device`` in ``[since, until]`` (or lifetime if both None)."""

    qs = Telemetry.objects.filter(device=device, is_on=True)
    if since is not None:
        qs = qs.filter(timestamp__gte=since)
    if until is not None:
        qs = qs.filter(timestamp__lte=until)

    rows = (
        qs.annotate(bucket=TruncMinute('timestamp'))
        .values('bucket')
        .annotate(avg_power=Avg('power_watts'))
    )
    watt_minutes = sum(float(r['avg_power'] or 0.0) for r in rows)
    return round(watt_minutes / 60.0 / 1000.0, 4)


def _to_uah(kwh: float, tariff: Decimal) -> float:
    """Convert kWh to UAH using the current tariff, rounded to kopecks."""

    if kwh <= 0:
        return 0.0
    return float((Decimal(str(kwh)) * tariff).quantize(Decimal('0.01')))


def compute_consumption_summary(
    since: datetime,
    until: datetime,
    devices: Iterable[Device] | None = None,
) -> ConsumptionSummary:
    """Build the Cabinet summary for the requested window."""

    tariff = SystemSettings.load().tariff_uah_per_kwh
    if devices is None:
        devices = list(Device.objects.all().order_by('priority', 'name'))
    else:
        devices = list(devices)

    summary = ConsumptionSummary(since=since, until=until, tariff_uah_per_kwh=tariff)
    for device in devices:
        period_kwh = _kwh_for_device(device, since, until)
        lifetime_kwh = _kwh_for_device(device, None, None)
        row = DeviceConsumption(
            device=device,
            period_kwh=period_kwh,
            lifetime_kwh=lifetime_kwh,
            period_uah=_to_uah(period_kwh, tariff),
            lifetime_uah=_to_uah(lifetime_kwh, tariff),
        )
        summary.devices.append(row)
        summary.period_kwh += period_kwh
        summary.lifetime_kwh += lifetime_kwh

    summary.period_kwh = round(summary.period_kwh, 4)
    summary.lifetime_kwh = round(summary.lifetime_kwh, 4)
    summary.period_uah = _to_uah(summary.period_kwh, tariff)
    summary.lifetime_uah = _to_uah(summary.lifetime_kwh, tariff)
    return summary

# Date-range parsing helpers used by AccountSummaryView.

DEFAULT_PERIOD_DAYS = 30
MAX_PERIOD_DAYS = 366


def resolve_period(since_str: str | None, until_str: str | None) -> tuple[datetime, datetime]:
    """Convert user-supplied YYYY-MM-DD strings to aware [since, until] datetimes."""

    tz = timezone.get_current_timezone()
    today_local = timezone.localdate()

    until_date = _parse_date(until_str, default=today_local, label='until')
    default_since = until_date - timedelta(days=DEFAULT_PERIOD_DAYS - 1)
    since_date = _parse_date(since_str, default=default_since, label='since')

    if since_date > until_date:
        raise ValueError('"since" must not be after "until".')

    span_days = (until_date - since_date).days + 1
    if span_days > MAX_PERIOD_DAYS:
        raise ValueError(f'Period must not exceed {MAX_PERIOD_DAYS} days.')

    since = timezone.make_aware(datetime.combine(since_date, datetime.min.time()), tz)
    until = timezone.make_aware(
        datetime.combine(until_date, datetime.max.time()), tz,
    )
    return since, until


def _parse_date(raw: str | None, *, default: date, label: str) -> date:
    if not raw:
        return default
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f'Invalid {label} date "{raw}" — expected YYYY-MM-DD.') from exc