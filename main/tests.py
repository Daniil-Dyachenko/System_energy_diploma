"""Tests for stage REST API and Logic(serializers, ingest endpoint, and the balancing algorithm.)"""
from __future__ import annotations

from datetime import timedelta

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.test import APIClient

from .models import BalancingEvent, Device, SystemSettings, Telemetry
from .services import rebalance_load

API_KEY = 'test-api-key'

@override_settings(DEVICE_API_KEY=API_KEY)
class TelemetryIngestTests(TestCase):
    """Covers the ESP32 uplink endpoint."""

    def setUp(self):
        self.client = APIClient()
        self.device = Device.objects.create(
            name='Boiler',
            device_id='esp32-boiler',
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
            HTTP_X_API_KEY=API_KEY,
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        self.assertEqual(Telemetry.objects.count(), 1)

        self.device.refresh_from_db()
        self.assertAlmostEqual(self.device.last_power_watts, 1500.0)
        self.assertIsNotNone(self.device.last_seen_at)

        body = resp.json()
        self.assertIn('balancing', body)
        self.assertEqual(body['balancing']['power_limit_watts'], 3000)

    def test_unknown_device_returns_400(self):
        resp = self.client.post(
            self.url,
            {'device_id': 'ghost', 'power_watts': 10.0},
            format='json',
            HTTP_X_API_KEY=API_KEY,
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_negative_power_rejected(self):
        resp = self.client.post(
            self.url,
            {'device_id': 'esp32-boiler', 'power_watts': -1.0},
            format='json',
            HTTP_X_API_KEY=API_KEY,
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


@override_settings(DEVICE_API_KEY=API_KEY)
class DeviceStateTests(TestCase):
    """Covers the ESP32 downlink endpoint."""

    def setUp(self):
        self.client = APIClient()
        self.fridge = Device.objects.create(
            name='Fridge', device_id='esp32-fridge', priority=1, is_on=True,
        )
        self.heater = Device.objects.create(
            name='Heater', device_id='esp32-heater', priority=8, is_on=False,
        )
        self.url = reverse('device-state')

    def test_lookup_single_device(self):
        resp = self.client.get(
            self.url,
            {'device_id': 'esp32-fridge'},
            HTTP_X_API_KEY=API_KEY,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json(), {'device_id': 'esp32-fridge', 'is_on': True})

    def test_lookup_unknown_device(self):
        resp = self.client.get(
            self.url,
            {'device_id': 'ghost'},
            HTTP_X_API_KEY=API_KEY,
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_list_all_devices(self):
        resp = self.client.get(self.url, HTTP_X_API_KEY=API_KEY)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        payload = resp.json()
        self.assertEqual(len(payload), 2)
        ids = {row['device_id'] for row in payload}
        self.assertEqual(ids, {'esp32-fridge', 'esp32-heater'})


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

        client = APIClient()
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

        client = APIClient()
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
        client = APIClient()
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
        self.client = APIClient()
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
        self.client = APIClient()
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
        self.client = APIClient()
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