from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class Device(models.Model):
    """Controllable appliance attached to an ESP32 relay."""

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
    shed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            'When the balancing algorithm last shed this device. Cleared on '
            'successful auto-restore or on manual ON via the toggle endpoint. '
            'Used to enforce the AUTO-mode restore cooldown.'
        ),
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
    """Power-consumption sample reported by an ESP32."""

    device = models.ForeignKey(
        Device,
        on_delete=models.CASCADE,
        related_name='telemetry',
    )
    power_watts = models.FloatField(
        validators=[MinValueValidator(0.0)],
        help_text='Instantaneous power draw in watts.',
    )
    is_on = models.BooleanField(
        default=True,
        help_text=(
            'Snapshot of `Device.is_on` at the moment this sample was ingested. '
        ),
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
    """Global runtime configuration for the load-balancing algorithm."""

    class RestoreMode(models.TextChoices):
        AUTO = 'AUTO', 'Auto-restore with cooldown'
        MANUAL = 'MANUAL', 'Manual restore only (latching)'

    power_limit_watts = models.PositiveIntegerField(
        default=3000,
        help_text='Maximum allowed total power draw across all active devices.',
    )
    is_active = models.BooleanField(
        default=True,
        help_text='If False, the balancing algorithm is paused.',
    )
    restore_mode = models.CharField(
        max_length=8,
        choices=RestoreMode.choices,
        default=RestoreMode.AUTO,
        help_text=(
            'AUTO: algorithm re-enables shed devices once load drops (with a '
            'cooldown). MANUAL: shed devices stay off until an user '
            'toggles them back on through the UI.'
        ),
    )
    restore_cooldown_seconds = models.PositiveIntegerField(
        default=30,
        help_text=(
            'In AUTO mode, the minimum number of seconds between when a device '
            'was shed and when it may be restored automatically.'
        ),
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'System settings'
        verbose_name_plural = 'System settings'

    def __str__(self) -> str:
        return f'Limit: {self.power_limit_watts} W'

    @classmethod
    def load(cls) -> 'SystemSettings':
        """Return the singleton row, creating it on first access."""
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj