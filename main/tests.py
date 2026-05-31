"""Tests for stage REST API and Logic(serializers, ingest endpoint, and the balancing algorithm.)"""
from __future__ import annotations

from datetime import timedelta

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.test import APIClient

from .models import BalancingEvent, Device, DeviceEvent, SystemSettings, Telemetry
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
        client = APIClient()
        client.post(reverse('device-toggle', kwargs={'pk': device.pk}))

        events = DeviceEvent.objects.filter(device=device)
        self.assertEqual(events.count(), 1)
        self.assertEqual(events.first().action, DeviceEvent.Action.MANUAL_ON)

    def test_manual_toggle_logs_manual_off(self):
        device = Device.objects.create(
            name='Iron', device_id='iron', priority=9,
            is_on=True, last_power_watts=800,
        )
        client = APIClient()
        client.post(reverse('device-toggle', kwargs={'pk': device.pk}))

        events = DeviceEvent.objects.filter(device=device)
        self.assertEqual(events.count(), 1)
        self.assertEqual(events.first().action, DeviceEvent.Action.MANUAL_OFF)


class DeviceHistoryEndpointTests(TestCase):
    """/api/devices/<id>/history/ payload shape + metric correctness."""

    def setUp(self):
        self.client = APIClient()
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
        client = APIClient()
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
        client = APIClient()
        resp = client.get(reverse('system-settings'))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn('tariff_uah_per_kwh', resp.json())
        self.assertEqual(resp.json()['tariff_uah_per_kwh'], '4.32')

    def test_post_settings_updates_tariff(self):
        """POST /api/settings/ with a new tariff persists the value."""
        from decimal import Decimal
        client = APIClient()
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
        client = APIClient()
        resp = client.post(
            reverse('system-settings'),
            {'tariff_uah_per_kwh': '-1.00'},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class AccountSummaryEndpointTests(TestCase):
    """GET /api/account/summary/ shape + period semantics."""

    def setUp(self):
        self.client = APIClient()
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