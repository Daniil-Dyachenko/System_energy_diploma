"""Load balancing across the ESP32-controlled devices.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable

from django.db import transaction
from django.utils import timezone

from .models import Device, SystemSettings, Telemetry


logger = logging.getLogger(__name__)


@dataclass
class BalancingReport:
    """Summary of what the algorithm changed during a single rebalance pass."""

    total_power_watts: float = 0.0
    power_limit_watts: int = 0
    is_overloaded: bool = False
    shed_devices: list[str] = field(default_factory=list)
    restored_devices: list[str] = field(default_factory=list)


def _current_total_load(devices: Iterable[Device]) -> float:
    """Sum the last known power draw across the devices that are ON."""
    return float(sum(d.last_power_watts for d in devices if d.is_on))


def _shed_one(devices: list[Device]) -> Device | None:
    """Turn off the on-device with the lowest priority (highest priority number).

    Returns the device that was switched off, or None if nothing could be shed.
    """
    candidates = [d for d in devices if d.is_on]
    if not candidates:
        return None
    victim = max(candidates, key=lambda d: (d.priority, d.last_power_watts))
    victim.is_on = False
    victim.save(update_fields=['is_on', 'updated_at'])
    return victim


def _restore_one(devices: list[Device], headroom_watts: float) -> Device | None:
    """Turn back on the highest-priority off-device that fits within headroom.
    """
    if headroom_watts <= 0:
        return None

    off_devices = [d for d in devices if not d.is_on and d.last_power_watts > 0]
    if not off_devices:
        return None

    off_devices.sort(key=lambda d: (d.priority, -d.last_power_watts))
    for device in off_devices:
        if device.last_power_watts <= headroom_watts:
            device.is_on = True
            device.save(update_fields=['is_on', 'updated_at'])
            return device
    return None


@transaction.atomic
def record_telemetry(device: Device, power_watts: float) -> Telemetry:
    """Persist a telemetry sample and refresh the device's live-state fields.
    """
    locked = Device.objects.select_for_update().get(pk=device.pk)
    locked.last_power_watts = float(power_watts)
    locked.last_seen_at = timezone.now()
    locked.save(update_fields=['last_power_watts', 'last_seen_at', 'updated_at'])

    sample = Telemetry.objects.create(device=locked, power_watts=power_watts)
    return sample


@transaction.atomic
def rebalance_load() -> BalancingReport:
    """Run the shed/restore loop and return what changed.
    """
    settings_row = SystemSettings.load()
    report = BalancingReport(
        power_limit_watts=settings_row.power_limit_watts,
    )

    if not settings_row.is_active:
        logger.info('Balancing skipped: SystemSettings.is_active=False')
        devices = list(Device.objects.select_for_update().all())
        report.total_power_watts = _current_total_load(devices)
        report.is_overloaded = report.total_power_watts > settings_row.power_limit_watts
        return report

    devices = list(Device.objects.select_for_update().all())
    total = _current_total_load(devices)

    while total > settings_row.power_limit_watts:
        victim = _shed_one(devices)
        if victim is None:
            break
        report.shed_devices.append(victim.device_id)
        total -= victim.last_power_watts
        logger.warning(
            'Shed device %s (priority=%d, draw=%.1fW); new total=%.1fW',
            victim.device_id, victim.priority, victim.last_power_watts, total,
        )

    while True:
        headroom = settings_row.power_limit_watts - total
        restored = _restore_one(devices, headroom)
        if restored is None:
            break
        report.restored_devices.append(restored.device_id)
        total += restored.last_power_watts
        logger.info(
            'Restored device %s (priority=%d, draw=%.1fW); new total=%.1fW',
            restored.device_id, restored.priority, restored.last_power_watts, total,
        )

    report.total_power_watts = total
    report.is_overloaded = total > settings_row.power_limit_watts
    return report


def ingest_and_rebalance(device: Device, power_watts: float) -> tuple[Telemetry, BalancingReport]:
    """High-level entry point used by the telemetry endpoint."""
    sample = record_telemetry(device, power_watts)
    report = rebalance_load()
    return sample, report