"""Concurrent telemetry flood — stress test for ``POST /api/telemetry/``.

Exit code is 0 only when the run stayed consistent and produced no errors, so it
doubles as a CI gate. Note: the telemetry throttle (120/min by default) caps how
many posts are processed; to measure raw throughput instead, temporarily widen
the 'telemetry' rate in config/settings.py.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django

django.setup()

from rest_framework.settings import api_settings
from main.models import Device, Telemetry


STRESS_PREFIX = 'stress-'


def make_stress_devices(count: int) -> list[Device]:
    """Create ``count`` throwaway devices for the flood."""

    devices = []
    for i in range(count):
        device, _ = Device.objects.update_or_create(
            device_id=f'{STRESS_PREFIX}{i}',
            defaults={
                'name': f'Stress {i}',
                'api_key': f'stress-key-{i}',
                'priority': Device.PRIORITY_MAX,
                'is_on': True,
            },
        )
        devices.append(device)
    return devices


def post_telemetry(url: str, device_id: str, api_key: str, watts: float, timeout: float):
    """Send one telemetry POST."""

    payload = json.dumps({'device_id': device_id, 'power_watts': watts}).encode()
    request = urllib.request.Request(url, data=payload, method='POST')
    request.add_header('Content-Type', 'application/json')
    request.add_header('X-API-Key', api_key)

    start = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            status = resp.status
            resp.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
    except Exception:
        status = 0
    return status, time.perf_counter() - start


def percentile(sorted_values: list[float], fraction: float) -> float:
    if not sorted_values:
        return 0.0
    index = min(len(sorted_values) - 1, int(len(sorted_values) * fraction))
    return sorted_values[index]


def main() -> None:
    parser = argparse.ArgumentParser(description='Concurrent telemetry flood.')
    parser.add_argument('--workers', type=int, default=20, help='concurrent threads')
    parser.add_argument('--requests', type=int, default=15, help='requests per worker')
    parser.add_argument('--devices', type=int, default=5, help='throwaway devices')
    parser.add_argument('--url', default='http://127.0.0.1:8000/api/telemetry/')
    parser.add_argument('--timeout', type=float, default=10.0)
    args = parser.parse_args()

    total = args.workers * args.requests
    rate = api_settings.DEFAULT_THROTTLE_RATES.get('telemetry')

    devices = make_stress_devices(args.devices)
    keys = [(d.device_id, d.api_key) for d in devices]
    before = Telemetry.objects.filter(device__in=devices).count()

    print(f'Stress test: POST {args.url}')
    print(f'  workers={args.workers}  requests/worker={args.requests}  '
          f'total={total}  devices={args.devices}')
    print(f'  telemetry throttle: {rate}')
    print('  running...')

    def run_worker(_: int):
        out = []
        for _ in range(args.requests):
            device_id, api_key = random.choice(keys)
            out.append(post_telemetry(
                args.url, device_id, api_key,
                round(random.uniform(0, 200), 1), args.timeout,
            ))
        return out

    healthy = False
    try:
        start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            batches = pool.map(run_worker, range(args.workers))
            results = [item for batch in batches for item in batch]
        wall = time.perf_counter() - start

        codes = [code for code, _ in results]
        ok = codes.count(201)
        throttled = codes.count(429)
        errors = sum(1 for code in codes if code == 0 or code >= 500)
        other = len(codes) - ok - throttled - errors

        ok_latencies_ms = sorted(lat * 1000 for code, lat in results if code == 201)
        after = Telemetry.objects.filter(device__in=devices).count()
        written = after - before
        consistent = written == ok

        print()
        print('Results')
        print(f'  duration:             {wall:.2f} s')
        print(f'  throughput:           {len(results) / wall:.1f} req/s')
        print(f'  HTTP 201 (ok):        {ok}')
        print(f'  HTTP 429 (throttled): {throttled}')
        print(f'  errors (5xx/conn):    {errors}')
        print(f'  other:                {other}')
        if ok_latencies_ms:
            avg = sum(ok_latencies_ms) / len(ok_latencies_ms)
            print()
            print('  Latency of processed (201) requests, ms:')
            print(f'    min={ok_latencies_ms[0]:.1f}  avg={avg:.1f}  '
                  f'p50={percentile(ok_latencies_ms, 0.50):.1f}  '
                  f'p95={percentile(ok_latencies_ms, 0.95):.1f}  '
                  f'max={ok_latencies_ms[-1]:.1f}')
        print()
        print(f'  consistency: {written} new Telemetry rows vs {ok} HTTP 201 '
              f'-> {"OK" if consistent else "MISMATCH"}')

        healthy = consistent and errors == 0 and other == 0
    finally:
        Device.objects.filter(pk__in=[d.pk for d in devices]).delete()
        print(f'  cleaned up {args.devices} stress device(s).')

    print()
    print('VERDICT:', 'PASS' if healthy else 'FAIL')
    sys.exit(0 if healthy else 1)


if __name__ == '__main__':
    main()