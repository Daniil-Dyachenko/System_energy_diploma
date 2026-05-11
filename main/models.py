from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class Device(models.Model):

    PRIORITY_MIN = 1
    PRIORITY_MAX = 10

    name = models.CharField(max_length=100)
    device_id = models.CharField(
        max_length=64,
        unique=True,
        help_text='Unique identifier reported by the ESP32 in API requests.',
    )
    priority = models.PositiveSmallIntegerField(
        default=5,
        validators=[
            MinValueValidator(PRIORITY_MIN),
            MaxValueValidator(PRIORITY_MAX),
        ],
        help_text='1 = highest priority (last to be shed), 10 = lowest.',
    )
    is_on = models.BooleanField(default=False)
    description = models.TextField(blank=True)
    last_power_watts = models.FloatField(
        default=0.0,
        validators=[MinValueValidator(0.0)],
        help_text='Most recent power reading reported by the device.',
    )
    last_seen_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text='Timestamp of the last telemetry packet received from the device.',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['priority', 'name']
        verbose_name = 'Device'
        verbose_name_plural = 'Devices'

    def __str__(self) -> str:
        state = 'ON' if self.is_on else 'OFF'
        return f'{self.name} (P{self.priority}, {state})'


class Telemetry(models.Model):

    device = models.ForeignKey(
        Device,
        on_delete=models.CASCADE,
        related_name='telemetry',
    )
    power_watts = models.FloatField(
        validators=[MinValueValidator(0.0)],
        help_text='Instantaneous power draw in watts.',
    )
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-timestamp']
        verbose_name = 'Telemetry sample'
        verbose_name_plural = 'Telemetry'
        indexes = [
            models.Index(fields=['device', '-timestamp']),
        ]

    def __str__(self) -> str:
        return f'{self.device.name} — {self.power_watts:.2f} W @ {self.timestamp:%Y-%m-%d %H:%M:%S}'


class SystemSettings(models.Model):

    power_limit_watts = models.PositiveIntegerField(
        default=3000,
        help_text='Maximum allowed total power draw across all active devices.',
    )
    is_active = models.BooleanField(
        default=True,
        help_text='If False, the balancing algorithm is paused.',
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'System settings'
        verbose_name_plural = 'System settings'

    def __str__(self) -> str:
        return f'Limit: {self.power_limit_watts} W'

    @classmethod
    def load(cls) -> 'SystemSettings':
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj