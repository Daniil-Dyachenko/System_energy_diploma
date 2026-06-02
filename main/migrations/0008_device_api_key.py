import secrets

import main.models
from django.db import migrations, models


def backfill_api_keys(apps, schema_editor):
    Device = apps.get_model('main', 'Device')
    for device in Device.objects.filter(api_key__isnull=True):
        device.api_key = secrets.token_hex(32)
        device.save(update_fields=['api_key'])


class Migration(migrations.Migration):

    dependencies = [
        ('main', '0007_systemsettings_tariff_uah_per_kwh'),
    ]

    operations = [
        migrations.AddField(
            model_name='device',
            name='api_key',
            field=models.CharField(max_length=64, null=True),
        ),
        migrations.RunPython(backfill_api_keys, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='device',
            name='api_key',
            field=models.CharField(
                default=main.models.generate_device_api_key,
                help_text='Per-device secret presented in the X-API-Key header. The board must send the key that belongs to the device_id it claims.',
                max_length=64,
                unique=True,
            ),
        ),
    ]