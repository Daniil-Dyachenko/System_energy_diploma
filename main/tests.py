"""Tests for stage REST API and Logic(serializers, ingest endpoint, and the balancing algorithm.)"""
from __future__ import annotations

import csv
import io
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework.throttling import SimpleRateThrottle

from .forecasting import (
    hourly_profile_forecast,
    linear_trend_forecast,
    moving_average_forecast,
)
from .models import BalancingEvent, Device, DeviceEvent, SystemSettings, Telemetry
from .services import rebalance_load
from .ui_views import ThrottledLoginView

def authed_client():
    """An APIClient authenticated as a throwaway user."""

    user, _ = User.objects.get_or_create(username='tester')
    client = APIClient()
    client.force_authenticate(user=user)
    return client

class TelemetryIngestTests(TestCase):
    """Covers the ESP32 uplink endpoint and its per-device X-API-Key auth."""

    def setUp(self):
        self.client = APIClient()
        self.device = Device.objects.create(
            name='Boiler',
            device_id='esp32-boiler',
            api_key='boiler-secret-key',
            priority=3,
            is_on=True,
        )
        self.url = reverse('telemetry-ingest')

    def test_rejects_request_without_api_key(self):
        resp = self.client.post(
            self.url,
            {'device_id': 'esp32-boiler', 'power_watts': 1500.0},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(Telemetry.objects.exists())

    def test_rejects_invalid_api_key(self):
        resp = self.client.post(
            self.url,
            {'device_id': 'esp32-boiler', 'power_watts': 1500.0},
            format='json',
            HTTP_X_API_KEY='wrong',
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_accepts_valid_payload_and_updates_device(self):
        resp = self.client.post(
            self.url,
            {'device_id': 'esp32-boiler', 'power_watts': 1500.0},
            format='json',
            HTTP_X_API_KEY=self.device.api_key,
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        self.assertEqual(Telemetry.objects.count(), 1)

        self.device.refresh_from_db()
        self.assertAlmostEqual(self.device.last_power_watts, 1500.0)
        self.assertIsNotNone(self.device.last_seen_at)

        body = resp.json()
        self.assertIn('balancing', body)
        self.assertEqual(body['balancing']['power_limit_watts'], 3000)

    def test_unknown_device_rejected_with_403(self):
        resp = self.client.post(
            self.url,
            {'device_id': 'ghost', 'power_watts': 10.0},
            format='json',
            HTTP_X_API_KEY=self.device.api_key,
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(Telemetry.objects.exists())

    def test_wrong_device_key_is_rejected(self):
        fridge = Device.objects.create(
            name='Fridge', device_id='esp32-fridge',
            api_key='fridge-secret-key', priority=1, is_on=True,
        )
        resp = self.client.post(
            self.url,
            {'device_id': 'esp32-boiler', 'power_watts': 1500.0},
            format='json',
            HTTP_X_API_KEY=fridge.api_key,
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(Telemetry.objects.exists())

    def test_negative_power_rejected(self):
        resp = self.client.post(
            self.url,
            {'device_id': 'esp32-boiler', 'power_watts': -1.0},
            format='json',
            HTTP_X_API_KEY=self.device.api_key,
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class DeviceStateTests(TestCase):
    """Covers the ESP32 downlink endpoint (per-device, device_id required)."""

    def setUp(self):
        self.client = APIClient()
        self.fridge = Device.objects.create(
            name='Fridge', device_id='esp32-fridge',
            api_key='fridge-secret-key', priority=1, is_on=True,
        )
        self.heater = Device.objects.create(
            name='Heater', device_id='esp32-heater',
            api_key='heater-secret-key', priority=8, is_on=False,
        )
        self.url = reverse('device-state')

    def test_lookup_own_device_with_matching_key(self):
        resp = self.client.get(
            self.url,
            {'device_id': 'esp32-fridge'},
            HTTP_X_API_KEY=self.fridge.api_key,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json(), {'device_id': 'esp32-fridge', 'is_on': True})

    def test_missing_device_id_rejected(self):
        resp = self.client.get(self.url, HTTP_X_API_KEY=self.fridge.api_key)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_unknown_device_rejected_with_403(self):
        resp = self.client.get(
            self.url, {'device_id': 'ghost'}, HTTP_X_API_KEY=self.fridge.api_key,
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_wrong_device_key_is_rejected(self):
        resp = self.client.get(
            self.url,
            {'device_id': 'esp32-heater'},
            HTTP_X_API_KEY=self.fridge.api_key,
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_each_device_reads_its_own_state(self):
        resp = self.client.get(
            self.url,
            {'device_id': 'esp32-heater'},
            HTTP_X_API_KEY=self.heater.api_key,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json(), {'device_id': 'esp32-heater', 'is_on': False})


class BalancingAlgorithmTests(TestCase):
    def setUp(self):
        SystemSettings.objects.update_or_create(
            pk=1, defaults={'power_limit_watts': 2000, 'is_active': True},
        )

    def _make(self, name, priority, is_on, watts):
        return Device.objects.create(
            name=name,
            device_id=name.lower(),
            priority=priority,
            is_on=is_on,
            last_power_watts=watts,
        )

    def test_overload_sheds_lowest_priority(self):
        fridge = self._make('Fridge', priority=1, is_on=True, watts=300)
        boiler = self._make('Boiler', priority=5, is_on=True, watts=1500)
        heater = self._make('Heater', priority=9, is_on=True, watts=800)

        report = rebalance_load()

        fridge.refresh_from_db()
        boiler.refresh_from_db()
        heater.refresh_from_db()

        self.assertTrue(fridge.is_on)
        self.assertTrue(boiler.is_on)
        self.assertFalse(heater.is_on, 'lowest-priority device must be shed first')
        self.assertIn('heater', report.shed_devices)
        self.assertFalse(report.is_overloaded)
        self.assertEqual(report.total_power_watts, 1800)

    def test_overload_sheds_multiple_when_needed(self):
        fridge = self._make('Fridge', priority=1, is_on=True, watts=300)
        boiler = self._make('Boiler', priority=5, is_on=True, watts=1900)
        heater = self._make('Heater', priority=9, is_on=True, watts=2200)

        rebalance_load()

        fridge.refresh_from_db()
        boiler.refresh_from_db()
        heater.refresh_from_db()
        self.assertTrue(fridge.is_on)
        self.assertFalse(boiler.is_on)
        self.assertFalse(heater.is_on)

    def test_slack_restores_highest_priority_first(self):
        self._make('Fridge', priority=1, is_on=True, watts=200)
        boiler = self._make('Boiler', priority=3, is_on=False, watts=1200)
        heater = self._make('Heater', priority=9, is_on=False, watts=500)

        rebalance_load()

        boiler.refresh_from_db()
        heater.refresh_from_db()
        self.assertTrue(boiler.is_on, 'higher-priority device should be restored first')
        self.assertTrue(heater.is_on)

    def test_restore_skips_device_that_would_overload(self):
        self._make('Fridge', priority=1, is_on=True, watts=900)
        boiler = self._make('Boiler', priority=3, is_on=False, watts=1500)

        rebalance_load()

        boiler.refresh_from_db()
        self.assertFalse(boiler.is_on)

    def test_inactive_settings_disables_balancing(self):
        SystemSettings.objects.filter(pk=1).update(is_active=False)
        boiler = self._make('Boiler', priority=5, is_on=True, watts=5000)

        report = rebalance_load()

        boiler.refresh_from_db()
        self.assertTrue(boiler.is_on, 'algorithm must not touch devices when paused')
        self.assertEqual(report.shed_devices, [])
        self.assertTrue(report.is_overloaded)


class CurrentLoadEndpointTests(TestCase):
    def test_snapshot_returns_devices_and_total(self):
        Device.objects.create(
            name='Fridge', device_id='esp32-fridge', priority=1,
            is_on=True, last_power_watts=300,
        )
        Device.objects.create(
            name='Heater', device_id='esp32-heater', priority=8,
            is_on=False, last_power_watts=2000,
        )
        client = APIClient()
        resp = client.get(reverse('current-load'))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        body = resp.json()
        self.assertEqual(body['total_power_watts'], 300)
        self.assertFalse(body['is_overloaded'])
        self.assertEqual(len(body['devices']), 2)

class RestoreModeTests(TestCase):
    """ MANUAL mode latches shedded devices off."""

    def setUp(self):
        self.settings_row, _ = SystemSettings.objects.update_or_create(
            pk=1,
            defaults={
                'power_limit_watts': 2000,
                'is_active': True,
                'restore_mode': SystemSettings.RestoreMode.MANUAL,
                'restore_cooldown_seconds': 30,
            },
        )

    def test_manual_mode_skips_restore(self):
        """In MANUAL mode a shed device must stay off even with comfortable headroom."""
        fridge = Device.objects.create(
            name='Fridge', device_id='fridge', priority=1,
            is_on=True, last_power_watts=500,
        )
        iron = Device.objects.create(
            name='Iron', device_id='iron', priority=9,
            is_on=False, last_power_watts=800,
            shed_at=timezone.now() - timedelta(hours=1),
        )

        report = rebalance_load()

        iron.refresh_from_db()
        fridge.refresh_from_db()
        self.assertFalse(iron.is_on, 'MANUAL mode must not auto-restore shed devices')
        self.assertEqual(report.restored_devices, [])
        self.assertEqual(report.restore_mode, SystemSettings.RestoreMode.MANUAL)
        self.assertIsNotNone(iron.shed_at)


class CooldownTests(TestCase):
    """AUTO restore is gated by `restore_cooldown_seconds`."""

    def setUp(self):
        SystemSettings.objects.update_or_create(
            pk=1,
            defaults={
                'power_limit_watts': 3000,
                'is_active': True,
                'restore_mode': SystemSettings.RestoreMode.AUTO,
                'restore_cooldown_seconds': 30,
            },
        )

    def test_shed_stamps_shed_at(self):
        """Algorithmic shed must record the timestamp used by the cooldown gate."""
        Device.objects.create(
            name='Fridge', device_id='fridge', priority=1,
            is_on=True, last_power_watts=1500,
        )
        iron = Device.objects.create(
            name='Iron', device_id='iron', priority=9,
            is_on=True, last_power_watts=2000,
        )

        before = timezone.now()
        rebalance_load()
        after = timezone.now()

        iron.refresh_from_db()
        self.assertFalse(iron.is_on)
        self.assertIsNotNone(iron.shed_at)
        self.assertTrue(before <= iron.shed_at <= after)

    def test_cooldown_blocks_restore(self):
        """A device shed within the cooldown window must not auto-restore yet."""
        Device.objects.create(
            name='Fridge', device_id='fridge', priority=1,
            is_on=True, last_power_watts=500,
        )
        iron = Device.objects.create(
            name='Iron', device_id='iron', priority=9,
            is_on=False, last_power_watts=800,
            shed_at=timezone.now() - timedelta(seconds=5),
        )

        report = rebalance_load()

        iron.refresh_from_db()
        self.assertFalse(iron.is_on, 'cooldown must block immediate restore')
        self.assertEqual(report.restored_devices, [])
        self.assertIsNotNone(iron.shed_at, 'cooldown gate must not silently reset shed_at')

    def test_cooldown_expires_allows_restore(self):
        """Once `restore_cooldown_seconds` has elapsed, AUTO restore re-enables the device."""
        Device.objects.create(
            name='Fridge', device_id='fridge', priority=1,
            is_on=True, last_power_watts=500,
        )
        iron = Device.objects.create(
            name='Iron', device_id='iron', priority=9,
            is_on=False, last_power_watts=800,
            shed_at=timezone.now() - timedelta(seconds=120),
        )

        report = rebalance_load()

        iron.refresh_from_db()
        self.assertTrue(iron.is_on, 'cooldown expired — device should be restored')
        self.assertIn('iron', report.restored_devices)
        self.assertIsNone(iron.shed_at, 'successful auto-restore must clear shed_at')


class ManualToggleTests(TestCase):
    """Manual toggle ON resets `shed_at` (bypasses cooldown)."""

    def test_manual_toggle_resets_shed_at(self):
        device = Device.objects.create(
            name='Iron', device_id='iron', priority=9,
            is_on=False, last_power_watts=800,
            shed_at=timezone.now() - timedelta(seconds=2),
        )
        client = authed_client()
        url = reverse('device-toggle', kwargs={'pk': device.pk})
        resp = client.post(url)

        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        device.refresh_from_db()
        self.assertTrue(device.is_on, 'toggle must flip the relay state')
        self.assertIsNone(device.shed_at, 'manual ON must clear shed_at to bypass cooldown')

    def test_manual_toggle_off_preserves_shed_at(self):
        """Flipping a device OFF manually must not stamp shed_at — that field
        is reserved for algorithmic shedding."""
        device = Device.objects.create(
            name='Iron', device_id='iron', priority=9,
            is_on=True, last_power_watts=800,
            shed_at=None,
        )

        client = authed_client()
        url = reverse('device-toggle', kwargs={'pk': device.pk})
        resp = client.post(url)

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        device.refresh_from_db()
        self.assertFalse(device.is_on)
        self.assertIsNone(device.shed_at)


class CurrentLoadEndpointTests(TestCase):
    def test_snapshot_returns_devices_and_total(self):
        Device.objects.create(
            name='Fridge', device_id='esp32-fridge', priority=1,
            is_on=True, last_power_watts=300,
        )
        Device.objects.create(
            name='Heater', device_id='esp32-heater', priority=8,
            is_on=False, last_power_watts=2000,
        )
        client = authed_client()
        resp = client.get(reverse('current-load'))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        body = resp.json()
        self.assertEqual(body['total_power_watts'], 300)
        self.assertFalse(body['is_overloaded'])
        self.assertEqual(len(body['devices']), 2)


class ChartDataEndpointTests(TestCase):
    """
    Verifies the two-step aggregation (AVG per device per minute, SUM across
    devices) and the is_on filter that excludes samples taken while a device
    was shed by the algorithm.
    """

    def setUp(self):
        self.client = authed_client()
        self.url = reverse('chart-data')

    def test_avg_then_sum_per_bucket(self):
        """Each bucket should be the sum of *per-device averages*, not the sum
        of every raw sample."""
        fridge = Device.objects.create(
            name='Fridge', device_id='f', priority=1, is_on=True,
        )
        boiler = Device.objects.create(
            name='Boiler', device_id='b', priority=3, is_on=True,
        )
        for _ in range(5):
            Telemetry.objects.create(device=fridge, power_watts=500, is_on=True)
            Telemetry.objects.create(device=boiler, power_watts=1500, is_on=True)

        resp = self.client.get(self.url, {'minutes': 5})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        buckets = resp.json()
        self.assertGreater(len(buckets), 0)
        for b in buckets:
            self.assertAlmostEqual(b['total_power_watts'], 2000.0, delta=1.0)

    def test_filters_out_shed_samples(self):
        """Samples ingested while is_on=False must be excluded from the chart."""
        fridge = Device.objects.create(
            name='Fridge', device_id='f', priority=1, is_on=True,
        )
        iron = Device.objects.create(
            name='Iron', device_id='i', priority=9, is_on=False,
        )
        for _ in range(3):
            Telemetry.objects.create(device=fridge, power_watts=500, is_on=True)
            Telemetry.objects.create(device=iron, power_watts=1500, is_on=False)

        resp = self.client.get(self.url, {'minutes': 5})
        buckets = resp.json()
        self.assertGreater(len(buckets), 0)
        for b in buckets:
            self.assertAlmostEqual(b['total_power_watts'], 500.0, delta=1.0)



class BalancingEventTests(TestCase):
    """Notifications: every algorithmic action persists an audit row."""

    def setUp(self):
        SystemSettings.objects.update_or_create(
            pk=1,
            defaults={
                'power_limit_watts': 2000,
                'is_active': True,
                'restore_mode': SystemSettings.RestoreMode.AUTO,
                'restore_cooldown_seconds': 30,
            },
        )

    def test_shed_creates_event(self):
        """A shed action emits a BalancingEvent with action=SHED + the totals snapshot."""
        Device.objects.create(
            name='Fridge', device_id='fridge', priority=1,
            is_on=True, last_power_watts=300,
        )
        Device.objects.create(
            name='Iron', device_id='iron', priority=9,
            is_on=True, last_power_watts=2200,
        )

        rebalance_load()

        events = list(BalancingEvent.objects.all())
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual(ev.action, BalancingEvent.Action.SHED)
        self.assertEqual(ev.device.device_id, 'iron')
        self.assertAlmostEqual(ev.device_power_watts, 2200)
        self.assertAlmostEqual(ev.total_power_watts, 300)
        self.assertEqual(ev.power_limit_watts, 2000)

    def test_restore_creates_event(self):
        """Auto-restore emits a BalancingEvent with action=RESTORE."""
        Device.objects.create(
            name='Fridge', device_id='fridge', priority=1,
            is_on=True, last_power_watts=500,
        )
        Device.objects.create(
            name='Iron', device_id='iron', priority=9,
            is_on=False, last_power_watts=800,
            shed_at=timezone.now() - timedelta(seconds=120),
        )

        rebalance_load()

        restores = list(BalancingEvent.objects.filter(action=BalancingEvent.Action.RESTORE))
        self.assertEqual(len(restores), 1)
        ev = restores[0]
        self.assertEqual(ev.device.device_id, 'iron')
        self.assertAlmostEqual(ev.device_power_watts, 800)
        self.assertAlmostEqual(ev.total_power_watts, 1300)
        self.assertEqual(ev.power_limit_watts, 2000)

    def test_inactive_logs_nothing(self):
        """When the algorithm is paused no events are recorded."""
        SystemSettings.objects.filter(pk=1).update(is_active=False)
        Device.objects.create(
            name='Iron', device_id='iron', priority=9,
            is_on=True, last_power_watts=5000,
        )

        rebalance_load()

        self.assertFalse(BalancingEvent.objects.exists())


class BalancingEventsEndpointTests(TestCase):
    """GET /api/balancing-events/ payload shape + limit handling."""

    def setUp(self):
        self.client = authed_client()
        self.url = reverse('balancing-events')
        self.device = Device.objects.create(
            name='Iron', device_id='iron', priority=9,
            is_on=False, last_power_watts=800,
        )

    def _make_event(self, action=BalancingEvent.Action.SHED):
        return BalancingEvent.objects.create(
            device=self.device,
            action=action,
            device_power_watts=800,
            total_power_watts=1500,
            power_limit_watts=2000,
        )

    def test_returns_recent_with_device_name(self):
        self._make_event()
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        body = resp.json()
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]['device_name'], 'Iron')
        self.assertEqual(body[0]['device_public_id'], 'iron')
        self.assertEqual(body[0]['action'], 'SHED')

    def test_respects_limit_param(self):
        for _ in range(10):
            self._make_event()
        resp = self.client.get(self.url, {'limit': 5})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.json()), 5)

    def test_orders_newest_first(self):
        first = self._make_event(action=BalancingEvent.Action.SHED)
        second = self._make_event(action=BalancingEvent.Action.RESTORE)
        resp = self.client.get(self.url)
        ids = [row['id'] for row in resp.json()]
        self.assertEqual(ids, [second.id, first.id])


class CurrentLoadLastOverloadTests(TestCase):
    """Notifications: /api/current-load/ exposes last_overload_at."""

    def setUp(self):
        self.client = authed_client()
        self.device = Device.objects.create(
            name='Iron', device_id='iron', priority=9,
            is_on=False, last_power_watts=800,
        )

    def test_null_when_no_overload_recorded(self):
        resp = self.client.get(reverse('current-load'))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIsNone(resp.json()['last_overload_at'])

    def test_reflects_latest_shed_event(self):
        BalancingEvent.objects.create(
            device=self.device, action=BalancingEvent.Action.RESTORE,
            device_power_watts=800, total_power_watts=1000, power_limit_watts=2000,
        )
        shed = BalancingEvent.objects.create(
            device=self.device, action=BalancingEvent.Action.SHED,
            device_power_watts=800, total_power_watts=1500, power_limit_watts=2000,
        )

        resp = self.client.get(reverse('current-load'))
        body = resp.json()
        self.assertIsNotNone(body['last_overload_at'])
        returned = parse_datetime(body['last_overload_at'])
        delta = abs((returned - shed.occurred_at).total_seconds())
        self.assertLess(delta, 1.0)

class DeviceEventLoggingTests(TestCase):
    """Device-detail: every relay state change persists a DeviceEvent."""

    def setUp(self):
        SystemSettings.objects.update_or_create(
            pk=1,
            defaults={
                'power_limit_watts': 2000,
                'is_active': True,
                'restore_mode': SystemSettings.RestoreMode.AUTO,
                'restore_cooldown_seconds': 30,
            },
        )

    def test_auto_shed_creates_device_event(self):
        Device.objects.create(
            name='Fridge', device_id='fridge', priority=1,
            is_on=True, last_power_watts=300,
        )
        iron = Device.objects.create(
            name='Iron', device_id='iron', priority=9,
            is_on=True, last_power_watts=2200,
        )

        rebalance_load()

        events = DeviceEvent.objects.filter(device=iron)
        self.assertEqual(events.count(), 1)
        self.assertEqual(events.first().action, DeviceEvent.Action.SHED)

    def test_auto_restore_creates_device_event(self):
        Device.objects.create(
            name='Fridge', device_id='fridge', priority=1,
            is_on=True, last_power_watts=500,
        )
        iron = Device.objects.create(
            name='Iron', device_id='iron', priority=9,
            is_on=False, last_power_watts=800,
            shed_at=timezone.now() - timedelta(seconds=120),
        )

        rebalance_load()

        restore = DeviceEvent.objects.filter(device=iron, action=DeviceEvent.Action.RESTORE)
        self.assertEqual(restore.count(), 1)

    def test_manual_toggle_logs_manual_on(self):
        device = Device.objects.create(
            name='Iron', device_id='iron', priority=9,
            is_on=False, last_power_watts=800,
        )
        client = authed_client()
        client.post(reverse('device-toggle', kwargs={'pk': device.pk}))

        events = DeviceEvent.objects.filter(device=device)
        self.assertEqual(events.count(), 1)
        self.assertEqual(events.first().action, DeviceEvent.Action.MANUAL_ON)

    def test_manual_toggle_logs_manual_off(self):
        device = Device.objects.create(
            name='Iron', device_id='iron', priority=9,
            is_on=True, last_power_watts=800,
        )
        client = authed_client()
        client.post(reverse('device-toggle', kwargs={'pk': device.pk}))

        events = DeviceEvent.objects.filter(device=device)
        self.assertEqual(events.count(), 1)
        self.assertEqual(events.first().action, DeviceEvent.Action.MANUAL_OFF)


class DeviceHistoryEndpointTests(TestCase):
    """/api/devices/<id>/history/ payload shape + metric correctness."""

    def setUp(self):
        self.client = authed_client()
        self.device = Device.objects.create(
            name='Boiler', device_id='boiler', priority=3,
            is_on=True, last_power_watts=1500,
        )
        for _ in range(5):
            Telemetry.objects.create(device=self.device, power_watts=1500, is_on=True)

    def _url(self, **params):
        base = reverse('device-history', kwargs={'pk': self.device.pk})
        if not params:
            return base
        from urllib.parse import urlencode
        return f'{base}?{urlencode(params)}'

    def test_returns_full_payload_shape(self):
        resp = self.client.get(self._url(window='1h'))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        body = resp.json()
        self.assertIn('device', body)
        self.assertEqual(body['device']['device_id'], 'boiler')
        self.assertEqual(body['window'], '1h')
        self.assertEqual(body['window_seconds'], 3600)
        self.assertIn('chart_data', body)
        self.assertIn('events', body)
        self.assertIn('metrics', body)
        for key in ('average_watts', 'peak_watts', 'on_time_seconds', 'energy_kwh'):
            self.assertIn(key, body['metrics'])
        self.assertNotIn('on_time_percent', body['metrics'])

    def test_avg_and_peak_reflect_telemetry(self):
        resp = self.client.get(self._url(window='1h'))
        m = resp.json()['metrics']
        self.assertAlmostEqual(m['average_watts'], 1500.0, delta=0.1)
        self.assertAlmostEqual(m['peak_watts'], 1500.0, delta=0.1)

    def test_invalid_window_defaults_to_1h(self):
        resp = self.client.get(self._url(window='garbage'))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json()['window_seconds'], 3600)

    def test_on_time_zero_when_device_off(self):
        """OFF device → on_time resets to 0 (the counter is current-session only)."""
        Device.objects.filter(pk=self.device.pk).update(is_on=False)
        resp = self.client.get(self._url(window='1h'))
        self.assertEqual(resp.json()['metrics']['on_time_seconds'], 0)

    def test_on_time_counts_from_last_on_event(self):
        """ON device → on_time = seconds since the most recent MANUAL_ON/RESTORE event."""
        now = timezone.now()
        old_on = DeviceEvent.objects.create(
            device=self.device, action=DeviceEvent.Action.MANUAL_ON, power_watts=0,
        )
        DeviceEvent.objects.filter(pk=old_on.pk).update(occurred_at=now - timedelta(hours=2))
        old_off = DeviceEvent.objects.create(
            device=self.device, action=DeviceEvent.Action.MANUAL_OFF, power_watts=0,
        )
        DeviceEvent.objects.filter(pk=old_off.pk).update(occurred_at=now - timedelta(hours=1))
        latest_on = DeviceEvent.objects.create(
            device=self.device, action=DeviceEvent.Action.MANUAL_ON, power_watts=0,
        )
        DeviceEvent.objects.filter(pk=latest_on.pk).update(occurred_at=now - timedelta(minutes=30))
        Device.objects.filter(pk=self.device.pk).update(is_on=True)

        resp = self.client.get(self._url(window='1h'))
        m = resp.json()['metrics']
        self.assertGreater(m['on_time_seconds'], 1700)
        self.assertLess(m['on_time_seconds'], 1900)

    def test_on_time_falls_back_to_created_at_without_events(self):
        """Fresh always-on device with no toggle events → counts from created_at."""
        now = timezone.now()
        Device.objects.filter(pk=self.device.pk).update(
            is_on=True,
            created_at=now - timedelta(minutes=45),
        )
        resp = self.client.get(self._url(window='1h'))
        m = resp.json()['metrics']
        self.assertGreater(m['on_time_seconds'], 2600)
        self.assertLess(m['on_time_seconds'], 2800)

    def test_on_time_resets_after_off_on_toggle(self):
        """Toggling OFF then ON again puts the counter back near zero."""
        client = authed_client()
        url = reverse('device-toggle', kwargs={'pk': self.device.pk})
        client.post(url)
        client.post(url)

        resp = self.client.get(self._url(window='1h'))
        m = resp.json()['metrics']
        self.assertLess(m['on_time_seconds'], 5)

    def test_chart_anchored_to_last_sample(self):
        """Quiet device: 5m window still includes the last samples even if they're old."""
        Telemetry.objects.filter(device=self.device).update(
            timestamp=timezone.now() - timedelta(minutes=30),
        )
        resp = self.client.get(self._url(window='5m'))
        chart = resp.json()['chart_data']
        self.assertGreater(len(chart), 0, 'pre-fix this would be empty')

    def test_energy_uses_actual_coverage_not_window(self):
        """A device with only 30 min of history shouldn't report a full 1h of energy."""
        Telemetry.objects.filter(device=self.device).delete()
        old = Telemetry.objects.create(device=self.device, power_watts=1500, is_on=True)
        Telemetry.objects.filter(pk=old.pk).update(
            timestamp=timezone.now() - timedelta(minutes=30),
        )
        Telemetry.objects.create(device=self.device, power_watts=1500, is_on=True)

        resp = self.client.get(self._url(window='1h'))
        m = resp.json()['metrics']
        self.assertGreater(m['energy_kwh'], 0.6)
        self.assertLess(m['energy_kwh'], 0.9)

    def test_events_returned_regardless_of_window(self):
        """Timeline shows recent toggles even when the chart window is narrow."""
        ev = DeviceEvent.objects.create(
            device=self.device, action=DeviceEvent.Action.MANUAL_ON, power_watts=0,
        )
        DeviceEvent.objects.filter(pk=ev.pk).update(
            occurred_at=timezone.now() - timedelta(days=1),
        )
        resp = self.client.get(self._url(window='5m'))
        events = resp.json()['events']
        self.assertEqual(len(events), 1)

    def test_unknown_device_returns_404(self):
        resp = self.client.get(
            reverse('device-history', kwargs={'pk': 99999})
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)


class TariffSettingsTests(TestCase):
    """tariff_uah_per_kwh defaults + persistence via /api/settings/."""

    def test_default_tariff_is_4_32(self):
        """Fresh SystemSettings row must default to the tariff 4.32."""
        from decimal import Decimal
        settings_row = SystemSettings.load()
        self.assertEqual(settings_row.tariff_uah_per_kwh, Decimal('4.32'))

    def test_get_settings_exposes_tariff(self):
        client = authed_client()
        resp = client.get(reverse('system-settings'))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn('tariff_uah_per_kwh', resp.json())
        self.assertEqual(resp.json()['tariff_uah_per_kwh'], '4.32')

    def test_post_settings_updates_tariff(self):
        """POST /api/settings/ with a new tariff persists the value."""
        from decimal import Decimal
        client = authed_client()
        resp = client.post(
            reverse('system-settings'),
            {'tariff_uah_per_kwh': '5.50'},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        SystemSettings.load().refresh_from_db()
        self.assertEqual(
            SystemSettings.load().tariff_uah_per_kwh,
            Decimal('5.50'),
        )

    def test_negative_tariff_rejected(self):
        client = authed_client()
        resp = client.post(
            reverse('system-settings'),
            {'tariff_uah_per_kwh': '-1.00'},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class AccountSummaryEndpointTests(TestCase):
    """GET /api/account/summary/ shape + period semantics."""

    def setUp(self):
        self.client = authed_client()
        self.url = reverse('account-summary')
        self.fridge = Device.objects.create(
            name='Fridge', device_id='fridge', priority=1,
            is_on=True, last_power_watts=300,
        )
        self.boiler = Device.objects.create(
            name='Boiler', device_id='boiler', priority=3,
            is_on=True, last_power_watts=1500,
        )

    def _seed(self, device, *, count, watts, is_on=True, when=None):
        for _ in range(count):
            sample = Telemetry.objects.create(
                device=device, power_watts=watts, is_on=is_on,
            )
            if when is not None:
                Telemetry.objects.filter(pk=sample.pk).update(timestamp=when)

    def test_default_window_is_last_30_days(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        body = resp.json()
        self.assertIn('period', body)
        self.assertEqual(body['period']['days'], 30)
        # tariff comes from defaults
        self.assertEqual(body['tariff_uah_per_kwh'], '4.32')

    def test_payload_shape(self):
        """All Cabinet UI fields are present and devices array is populated."""
        resp = self.client.get(self.url)
        body = resp.json()
        for key in (
            'period', 'tariff_uah_per_kwh',
            'period_kwh', 'period_uah', 'lifetime_kwh', 'lifetime_uah',
            'devices',
        ):
            self.assertIn(key, body)
        self.assertEqual(len(body['devices']), 2)
        for row in body['devices']:
            for key in (
                'id', 'name', 'device_id', 'priority', 'is_on',
                'period_kwh', 'period_uah', 'lifetime_kwh', 'lifetime_uah',
            ):
                self.assertIn(key, row)

    def test_since_after_until_returns_400(self):
        resp = self.client.get(
            self.url, {'since': '2026-06-01', 'until': '2026-05-01'},
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('detail', resp.json())

    def test_malformed_date_returns_400(self):
        resp = self.client.get(self.url, {'since': 'not-a-date'})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_lifetime_includes_samples_outside_window(self):
        """Telemetry from before the period must still count toward lifetime."""
        self._seed(self.boiler, count=60, watts=1500)
        old_ts = timezone.now() - timedelta(days=180)
        self._seed(self.boiler, count=1, watts=2000, when=old_ts)

        resp = self.client.get(self.url)
        body = resp.json()
        boiler_row = next(d for d in body['devices'] if d['name'] == 'Boiler')
        self.assertGreater(boiler_row['lifetime_kwh'], boiler_row['period_kwh'])

    def test_cost_equals_kwh_times_tariff(self):
        """UAH values must equal kWh * tariff for each row + the totals."""
        from decimal import Decimal
        SystemSettings.objects.update_or_create(
            pk=1, defaults={'tariff_uah_per_kwh': Decimal('5.00')},
        )
        self._seed(self.fridge, count=60, watts=600)
        self._seed(self.boiler, count=60, watts=1200)

        resp = self.client.get(self.url)
        body = resp.json()
        for row in body['devices']:
            expected = round(row['period_kwh'] * 5.0, 2)
            self.assertAlmostEqual(row['period_uah'], expected, places=2)
        self.assertAlmostEqual(
            body['period_uah'],
            round(body['period_kwh'] * 5.0, 2),
            places=2,
        )

    def test_shed_samples_excluded_from_energy(self):
        """Samples ingested while device was algorithmically shed must not count."""
        self._seed(self.fridge, count=10, watts=500, is_on=True)
        self._seed(self.fridge, count=10, watts=500, is_on=False)

        resp = self.client.get(self.url)
        fridge_row = next(d for d in resp.json()['devices'] if d['name'] == 'Fridge')
        self.assertGreater(fridge_row['period_kwh'], 0)
        self.assertLess(fridge_row['period_kwh'], 0.02)

    def test_empty_database_returns_zeros(self):
        """No devices, no telemetry, then totals are 0 but payload still valid."""
        Telemetry.objects.all().delete()
        Device.objects.all().delete()
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        body = resp.json()
        self.assertEqual(body['period_kwh'], 0.0)
        self.assertEqual(body['lifetime_kwh'], 0.0)
        self.assertEqual(body['period_uah'], 0.0)
        self.assertEqual(body['devices'], [])


class AccountExportEndpointTests(TestCase):
    """GET /api/account/export/ returns a CSV of consumption."""

    def setUp(self):
        self.client = authed_client()
        self.url = reverse('account-export')
        self.fridge = Device.objects.create(
            name='Fridge', device_id='fridge', priority=1,
            is_on=True, last_power_watts=300,
        )
        self.boiler = Device.objects.create(
            name='Boiler', device_id='boiler', priority=3,
            is_on=False, last_power_watts=1500,
        )

    def _rows(self, resp):
        text = resp.content.decode('windows-1251')
        return list(csv.reader(io.StringIO(text), delimiter=';'))

    def test_returns_csv_attachment(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn('text/csv', resp['Content-Type'])
        self.assertIn('attachment', resp['Content-Disposition'])
        self.assertIn('.csv', resp['Content-Disposition'])

    def test_body_is_windows1251(self):
        resp = self.client.get(self.url)
        self.assertFalse(resp.content.startswith(b'\xef\xbb\xbf'))
        self.assertIn('charset=windows-1251', resp['Content-Type'])
        self.assertIn('Прилад', resp.content.decode('windows-1251'))

    def test_header_row_per_device_and_totals(self):
        rows = self._rows(self.client.get(self.url))
        self.assertEqual(rows[0][0], 'Прилад')
        self.assertIn('кВт·год (період)', rows[0])
        names = [r[0] for r in rows if r]
        self.assertIn('Fridge', names)
        self.assertIn('Boiler', names)
        self.assertIn('Всього', names)

    def test_status_column_reflects_is_on(self):
        by_name = {r[0]: r for r in self._rows(self.client.get(self.url)) if r}
        self.assertEqual(by_name['Fridge'][3], 'Увімкнено')
        self.assertEqual(by_name['Boiler'][3], 'Вимкнено')

    def test_decimals_use_comma_separator(self):
        Telemetry.objects.create(device=self.fridge, power_watts=600, is_on=True)
        by_name = {r[0]: r for r in self._rows(self.client.get(self.url)) if r}
        kwh_cell = by_name['Fridge'][4]
        self.assertIn(',', kwh_cell)
        self.assertNotIn('.', kwh_cell)

    def test_bad_date_returns_400(self):
        resp = self.client.get(self.url, {'since': 'not-a-date'})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('detail', resp.json())


class ForecastAlgorithmTests(TestCase):
    """The three pure prediction functions in isolation."""

    def test_moving_average_holds_trailing_mean(self):
        out = moving_average_forecast([100, 200, 300, 400], horizon=3, window=2)
        self.assertEqual(out, [350.0, 350.0, 350.0])

    def test_moving_average_clamps_window_to_history(self):
        out = moving_average_forecast([100, 200], horizon=1, window=10)
        self.assertEqual(out, [150.0])

    def test_moving_average_empty_history_is_zero(self):
        self.assertEqual(moving_average_forecast([], horizon=3, window=6), [0.0, 0.0, 0.0])

    def test_linear_trend_extrapolates_a_clean_line(self):
        out = linear_trend_forecast([10, 20, 30, 40, 50], horizon=3)
        self.assertEqual(out, [60.0, 70.0, 80.0])

    def test_linear_trend_clamps_negative_projection(self):
        out = linear_trend_forecast([100, 50, 0], horizon=2)
        self.assertEqual(out, [0.0, 0.0])

    def test_linear_trend_single_point_is_flat(self):
        self.assertEqual(linear_trend_forecast([500], horizon=2), [500.0, 500.0])

    def test_hourly_profile_maps_hour_of_day(self):
        """Each future hour gets the historical average for that hour-of-day."""
        def at(days_ago, hour):
            base = timezone.localtime(timezone.now()).replace(
                hour=hour, minute=0, second=0, microsecond=0,
            )
            return base - timedelta(days=days_ago)

        history = [
            (at(2, 8), 100.0),
            (at(1, 8), 200.0),
            (at(1, 20), 900.0),
        ]
        future = [at(-1, 8), at(-1, 20)]
        out = hourly_profile_forecast(history, future)
        self.assertEqual(out, [150.0, 900.0])

    def test_hourly_profile_empty_history_is_zero(self):
        future = [timezone.now() + timedelta(hours=1)]
        self.assertEqual(hourly_profile_forecast([], future), [0.0])


class ForecastEndpointTests(TestCase):
    """GET /api/forecast/ shape + parameter handling."""

    def setUp(self):
        self.client = authed_client()
        self.url = reverse('forecast')
        self.device = Device.objects.create(
            name='Boiler', device_id='boiler', priority=3,
            is_on=True, last_power_watts=500,
        )

    def _seed(self, *, count, watts, is_on=True):
        for _ in range(count):
            Telemetry.objects.create(device=self.device, power_watts=watts, is_on=is_on)

    def test_payload_shape(self):
        self._seed(count=5, watts=500)
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        body = resp.json()
        for key in (
            'generated_at', 'horizon_hours', 'history_days', 'granularity',
            'power_limit_watts', 'tariff_uah_per_kwh', 'recommended_method',
            'history', 'methods',
        ):
            self.assertIn(key, body)
        self.assertEqual(len(body['methods']), 3)
        keys = {m['key'] for m in body['methods']}
        self.assertEqual(keys, {'moving_average', 'linear_trend', 'hourly_profile'})
        for m in body['methods']:
            for key in (
                'key', 'label', 'description', 'points', 'energy_kwh',
                'energy_uah', 'predicted_peak_watts', 'predicted_overload',
                'mae_watts', 'mape_percent',
            ):
                self.assertIn(key, m)

    def test_default_horizon_is_24h(self):
        self._seed(count=3, watts=500)
        body = self.client.get(self.url).json()
        self.assertEqual(body['horizon_hours'], 24)
        for m in body['methods']:
            self.assertEqual(len(m['points']), 24)

    def test_custom_horizon_controls_point_count(self):
        self._seed(count=3, watts=500)
        body = self.client.get(self.url, {'hours': 6}).json()
        self.assertEqual(body['horizon_hours'], 6)
        for m in body['methods']:
            self.assertEqual(len(m['points']), 6)

    def test_bad_horizon_returns_400(self):
        for bad in ('0', 'abc', '999'):
            resp = self.client.get(self.url, {'hours': bad})
            self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, bad)
            self.assertIn('detail', resp.json())

    def test_bad_history_days_returns_400(self):
        resp = self.client.get(self.url, {'history_days': '999'})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_history_excludes_shed_samples(self):
        """Only is_on=True telemetry feeds the model — same filter as the chart."""
        self._seed(count=5, watts=1500, is_on=False)
        body = self.client.get(self.url).json()
        self.assertEqual(body['history'], [])
        self.assertIsNone(body['recommended_method'])

    def test_empty_database_returns_valid_payload(self):
        Telemetry.objects.all().delete()
        Device.objects.all().delete()
        body = self.client.get(self.url).json()
        self.assertEqual(body['history'], [])
        self.assertIsNone(body['recommended_method'])
        self.assertEqual(len(body['methods']), 3)
        for m in body['methods']:
            self.assertEqual(len(m['points']), 24)
            self.assertEqual(m['energy_kwh'], 0.0)

    def test_predicted_overload_flag(self):
        """A load well above a low limit must raise predicted_overload."""
        SystemSettings.objects.update_or_create(
            pk=1, defaults={'power_limit_watts': 50},
        )
        self._seed(count=5, watts=500)
        body = self.client.get(self.url).json()
        ma = next(m for m in body['methods'] if m['key'] == 'moving_average')
        self.assertTrue(ma['predicted_overload'])
        self.assertGreater(ma['predicted_peak_watts'], 50)


class EndpointAuthTests(TestCase):
    """Stage 5: the web/API surface rejects anonymous callers."""

    def setUp(self):
        self.anon = APIClient()
        self.device = Device.objects.create(
            name='Fridge', device_id='fridge', priority=1,
            is_on=True, last_power_watts=300,
        )

    def test_web_endpoints_reject_anonymous(self):
        urls = [
            reverse('current-load'),
            reverse('chart-data'),
            reverse('system-settings'),
            reverse('balancing-events'),
            reverse('account-summary'),
            reverse('account-export'),
            reverse('forecast'),
            reverse('device-list'),
            reverse('device-history', kwargs={'pk': self.device.pk}),
        ]
        for url in urls:
            resp = self.anon.get(url)
            self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN, url)

    def test_settings_post_rejects_anonymous(self):
        resp = self.anon.post(
            reverse('system-settings'), {'power_limit_watts': 1234}, format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_device_toggle_rejects_anonymous(self):
        resp = self.anon.post(reverse('device-toggle', kwargs={'pk': self.device.pk}))
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.device.refresh_from_db()
        self.assertTrue(self.device.is_on, 'anonymous toggle must not flip the relay')

    def test_authenticated_access_succeeds(self):
        resp = authed_client().get(reverse('current-load'))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_esp32_endpoint_still_guarded_by_api_key(self):
        resp = self.anon.get(reverse('device-state'))
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


class ApiThrottleTests(TestCase):
    """DRF caps a flood on the ESP32 telemetry endpoint."""

    def setUp(self):
        cache.clear()
        self.device = Device.objects.create(
            name='Boiler', device_id='b', api_key='k', is_on=True,
        )
        self.url = reverse('telemetry-ingest')

    def test_telemetry_returns_429_after_limit(self):
        rates = {'anon': '30/min', 'user': '300/min', 'telemetry': '3/min', 'device_state': '180/min'}
        with mock.patch.object(SimpleRateThrottle, 'THROTTLE_RATES', rates):
            client = APIClient()
            codes = [
                client.post(
                    self.url, {'device_id': 'b', 'power_watts': 100},
                    format='json', HTTP_X_API_KEY='k',
                ).status_code
                for _ in range(5)
            ]
        self.assertEqual(codes[:3], [201, 201, 201], codes)
        self.assertEqual(codes[3], status.HTTP_429_TOO_MANY_REQUESTS, codes)


class LoginThrottleTests(TestCase):
    """The login form locks an IP out after repeated failures."""

    PASSWORD = 'correct-horse-battery'

    def setUp(self):
        cache.clear()
        User.objects.create_user('operator', password=self.PASSWORD)
        self.url = reverse('login')

    def test_lockout_after_repeated_failures(self):
        for _ in range(ThrottledLoginView.MAX_ATTEMPTS):
            resp = self.client.post(
                self.url, {'username': 'operator', 'password': 'nope'},
            )
            self.assertEqual(resp.status_code, 200)

        resp = self.client.post(
            self.url, {'username': 'operator', 'password': self.PASSWORD},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Забагато')
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_successful_login_clears_counter(self):
        self.client.post(self.url, {'username': 'operator', 'password': 'nope'})
        resp = self.client.post(
            self.url, {'username': 'operator', 'password': self.PASSWORD},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn('_auth_user_id', self.client.session)


class CsrfProtectionTests(TestCase):
    """Stage 5 security: session-authenticated writes require a CSRF token."""

    def setUp(self):
        self.user = User.objects.create_user('op', password='pw')
        self.settings_url = reverse('system-settings')

    def _session_client(self):
        client = APIClient(enforce_csrf_checks=True)
        client.force_login(self.user)
        return client

    def test_session_post_without_csrf_token_is_rejected(self):
        client = self._session_client()
        resp = client.post(self.settings_url, {'power_limit_watts': 1234}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_session_get_does_not_require_csrf(self):
        client = self._session_client()
        resp = client.get(self.settings_url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_session_post_with_csrf_token_succeeds(self):
        client = self._session_client()
        client.get(reverse('dashboard'))
        token = client.cookies['csrftoken'].value
        resp = client.post(
            self.settings_url, {'power_limit_watts': 1234},
            format='json', HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)


class ApiMassAssignmentTests(TestCase):
    """Stage 5 security: clients cannot write server-computed device fields."""

    def test_readonly_fields_are_ignored_on_patch(self):
        device = Device.objects.create(
            name='Fridge', device_id='fridge', priority=5, is_on=True,
        )
        client = authed_client()
        resp = client.patch(
            reverse('device-detail', kwargs={'pk': device.pk}),
            {'last_power_watts': 9999, 'shed_at': '2020-01-01T00:00:00Z'},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        device.refresh_from_db()
        self.assertEqual(device.last_power_watts, 0.0)
        self.assertIsNone(device.shed_at)

