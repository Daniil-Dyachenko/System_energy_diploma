"""Short-term consumption forecasting for the total system load."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Callable

from django.db.models import Avg
from django.db.models.functions import TruncHour
from django.utils import timezone

from .models import SystemSettings, Telemetry


DEFAULT_HORIZON_HOURS = 24
MAX_HORIZON_HOURS = 168
DEFAULT_HISTORY_DAYS = 7
MAX_HISTORY_DAYS = 30
DEFAULT_MA_WINDOW = 6
MAX_MA_WINDOW = 168
DEFAULT_TREND_POINTS = 24
MAX_TREND_POINTS = 720

_FALLBACK_RECOMMENDED = 'moving_average'

Point = tuple[datetime, float]
PredictFn = Callable[[list[Point], list[datetime]], list[float]]


# Pure forecasting algorithms (no Django dependency beyond timezone helpers).

def moving_average_forecast(values: list[float], horizon: int, window: int) -> list[float]:
    """Flat projection at the mean of the last ``window`` observations."""
    if horizon <= 0:
        return []
    if not values:
        return [0.0] * horizon
    w = max(1, min(window, len(values)))
    avg = max(0.0, sum(values[-w:]) / w)
    return [avg] * horizon


def linear_trend_forecast(
    values: list[float], horizon: int, num_points: int | None = None,
) -> list[float]:
    """Ordinary-least-squares line through the last ``num_points`` values."""

    if horizon <= 0:
        return []
    pts = values[-num_points:] if num_points else list(values)
    n = len(pts)
    if n == 0:
        return [0.0] * horizon
    if n == 1:
        return [max(0.0, pts[0])] * horizon

    mean_x = (n - 1) / 2.0
    mean_y = sum(pts) / n
    denom = sum((x - mean_x) ** 2 for x in range(n))
    slope = (
        sum((x - mean_x) * (pts[x] - mean_y) for x in range(n)) / denom
        if denom else 0.0
    )
    intercept = mean_y - slope * mean_x
    return [max(0.0, intercept + slope * ((n - 1) + h)) for h in range(1, horizon + 1)]


def hourly_profile_forecast(
    history_points: list[Point], future_times: list[datetime],
) -> list[float]:
    """Average-by-hour-of-day profile."""

    if not future_times:
        return []
    if not history_points:
        return [0.0] * len(future_times)

    by_hour: dict[int, list[float]] = defaultdict(list)
    total = 0.0
    for dt, value in history_points:
        by_hour[timezone.localtime(dt).hour].append(value)
        total += value
    overall = total / len(history_points)
    hour_mean = {hour: sum(vals) / len(vals) for hour, vals in by_hour.items()}

    return [
        max(0.0, hour_mean.get(timezone.localtime(ft).hour, overall))
        for ft in future_times
    ]


# Method registry

@dataclass
class _Method:
    key: str
    label: str
    description: str
    predict: PredictFn


def _build_methods(ma_window: int, trend_points: int) -> list[_Method]:
    """Instantiate the three methods, baking in the window/points settings."""
    return [
        _Method(
            key='moving_average',
            label='Ковзне середнє',
            description=(
                f'Прогноз дорівнює середньому споживанню за останні {ma_window} '
                'год і тримається рівним. Добре працює для стабільного '
                'навантаження, але не вловлює тренд чи добовий ритм.'
            ),
            predict=lambda hp, ft: moving_average_forecast(
                [v for _, v in hp], len(ft), ma_window,
            ),
        ),
        _Method(
            key='linear_trend',
            label='Лінійний тренд (МНК)',
            description=(
                f'Метод найменших квадратів по останніх {trend_points} точках. '
                'Будує пряму й продовжує її вперед - вловлює поступове '
                'зростання або спад споживання.'
            ),
            predict=lambda hp, ft: linear_trend_forecast(
                [v for _, v in hp], len(ft), trend_points,
            ),
        ),
        _Method(
            key='hourly_profile',
            label='Профіль по годинах доби',
            description=(
                'Середнє споживання для кожної години доби, спроєктоване '
                'вперед. Вловлює добовий ритм - ранкові та вечірні піки, '
                'нічний спад.'
            ),
            predict=hourly_profile_forecast,
        ),
    ]


# Data loading + orchestration

def load_hourly_history(since: datetime, until: datetime) -> list[Point]:
    """Total system power per hour in ``[since, until]`` (only ``is_on`` samples)."""
    rows = (
        Telemetry.objects
        .filter(timestamp__gte=since, timestamp__lte=until, is_on=True)
        .annotate(bucket=TruncHour('timestamp'))
        .values('bucket', 'device_id')
        .annotate(avg_power=Avg('power_watts'))
    )
    buckets: dict[datetime, float] = defaultdict(float)
    for row in rows:
        buckets[row['bucket']] += float(row['avg_power'] or 0.0)
    return [(bucket, round(buckets[bucket], 4)) for bucket in sorted(buckets)]


def _to_uah(kwh: float, tariff: Decimal) -> float:
    """kWh → UAH at the given tariff."""
    if kwh <= 0:
        return 0.0
    return float((Decimal(str(kwh)) * tariff).quantize(Decimal('0.01')))


def _backtest(
    method: _Method, history_points: list[Point], max_horizon: int,
) -> tuple[float | None, float | None]:
    """Hold out the tail of the history, predict it, score MAE + MAPE."""

    n = len(history_points)
    test_n = min(max_horizon, n // 2)
    if test_n < 1:
        return None, None

    train = history_points[:-test_n]
    test = history_points[-test_n:]
    actuals = [v for _, v in test]
    preds = method.predict(train, [t for t, _ in test])

    abs_errors = [abs(p - a) for p, a in zip(preds, actuals)]
    mae = round(sum(abs_errors) / len(abs_errors), 2) if abs_errors else None

    nonzero = [(p, a) for p, a in zip(preds, actuals) if a > 1e-9]
    mape = (
        round(sum(abs(p - a) / a for p, a in nonzero) / len(nonzero) * 100.0, 1)
        if nonzero else None
    )
    return mae, mape


@dataclass
class ForecastMethodResult:
    """One method's forecast + its backtest score, as shown in the UI."""

    key: str
    label: str
    description: str
    points: list[dict] = field(default_factory=list)
    energy_kwh: float = 0.0
    energy_uah: float = 0.0
    predicted_peak_watts: float = 0.0
    predicted_overload: bool = False
    mae_watts: float | None = None
    mape_percent: float | None = None


@dataclass
class ForecastResult:
    """Full payload returned by :func:`compute_forecast`."""

    generated_at: datetime
    horizon_hours: int
    history_days: int
    granularity: str
    power_limit_watts: int
    tariff_uah_per_kwh: Decimal
    recommended_method: str | None
    history: list[dict] = field(default_factory=list)
    methods: list[ForecastMethodResult] = field(default_factory=list)


def compute_forecast(
    *,
    now: datetime | None = None,
    horizon_hours: int = DEFAULT_HORIZON_HOURS,
    history_days: int = DEFAULT_HISTORY_DAYS,
    ma_window: int = DEFAULT_MA_WINDOW,
    trend_points: int = DEFAULT_TREND_POINTS,
) -> ForecastResult:
    now = now or timezone.now()
    settings_row = SystemSettings.load()
    tariff = settings_row.tariff_uah_per_kwh
    limit = settings_row.power_limit_watts

    since = now - timedelta(days=history_days)
    history_points = load_hourly_history(since, now)

    current_hour = now.replace(minute=0, second=0, microsecond=0)
    future_times = [current_hour + timedelta(hours=i + 1) for i in range(horizon_hours)]

    methods = _build_methods(ma_window, trend_points)
    results: list[ForecastMethodResult] = []
    for method in methods:
        preds = method.predict(history_points, future_times)
        energy_kwh = round(sum(preds) / 1000.0, 4)
        mae, mape = _backtest(method, history_points, horizon_hours)
        results.append(ForecastMethodResult(
            key=method.key,
            label=method.label,
            description=method.description,
            points=[
                {'timestamp': ft, 'power_watts': round(p, 2)}
                for ft, p in zip(future_times, preds)
            ],
            energy_kwh=energy_kwh,
            energy_uah=_to_uah(energy_kwh, tariff),
            predicted_peak_watts=round(max(preds), 2) if preds else 0.0,
            predicted_overload=any(p > limit for p in preds),
            mae_watts=mae,
            mape_percent=mape,
        ))

    return ForecastResult(
        generated_at=now,
        horizon_hours=horizon_hours,
        history_days=history_days,
        granularity='hour',
        power_limit_watts=limit,
        tariff_uah_per_kwh=tariff,
        recommended_method=_pick_recommended(results, has_history=bool(history_points)),
        history=[{'timestamp': ts, 'power_watts': val} for ts, val in history_points],
        methods=results,
    )


def _pick_recommended(results: list[ForecastMethodResult], *, has_history: bool) -> str | None:
    """Lowest-MAE method; falls back to a safe default when no backtest ran."""
    graded = [r for r in results if r.mae_watts is not None]
    if graded:
        return min(graded, key=lambda r: r.mae_watts).key
    return _FALLBACK_RECOMMENDED if has_history else None


# Query-parameter parsing for ForecastView.

def resolve_forecast_params(query) -> dict:
    """Validate the forecast query params, raising ValueError on bad input."""

    return {
        'horizon_hours': _parse_int(
            query.get('hours'), DEFAULT_HORIZON_HOURS, 1, MAX_HORIZON_HOURS, 'hours',
        ),
        'history_days': _parse_int(
            query.get('history_days'), DEFAULT_HISTORY_DAYS, 1, MAX_HISTORY_DAYS, 'history_days',
        ),
        'ma_window': _parse_int(
            query.get('ma_window'), DEFAULT_MA_WINDOW, 1, MAX_MA_WINDOW, 'ma_window',
        ),
        'trend_points': _parse_int(
            query.get('trend_points'), DEFAULT_TREND_POINTS, 2, MAX_TREND_POINTS, 'trend_points',
        ),
    }


def _parse_int(raw, default: int, lo: int, hi: int, label: str) -> int:
    if raw is None or raw == '':
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'Invalid "{label}" — expected an integer.') from exc
    if not (lo <= value <= hi):
        raise ValueError(f'"{label}" must be between {lo} and {hi}.')
    return value