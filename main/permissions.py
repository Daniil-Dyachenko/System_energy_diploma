"""Custom DRF permission classes."""
from __future__ import annotations

import hmac

from rest_framework.permissions import BasePermission

from .models import Device

API_KEY_HEADER = 'HTTP_X_API_KEY'


class HasDeviceApiKey(BasePermission):
    """Authenticate an ESP32 request with that specific device's static token.
    """

    message = 'Invalid or missing X-API-Key header for the requested device.'

    def has_permission(self, request, view) -> bool:
        provided = request.META.get(API_KEY_HEADER, '') or ''
        if not provided:
            return False

        device_id = self._requested_device_id(request)
        if not device_id:
            return False

        try:
            device = Device.objects.get(device_id=device_id)
        except Device.DoesNotExist:
            return False

        if not device.api_key:
            return False

        if not hmac.compare_digest(device.api_key, provided):
            return False

        request.auth_device = device
        return True

    @staticmethod
    def _requested_device_id(request) -> str:
        """Pull the claimed device_id from the body (uplink) or query (downlink).
        """
        data = getattr(request, 'data', None)
        if isinstance(data, dict):
            device_id = data.get('device_id')
            if device_id:
                return str(device_id)
        return request.query_params.get('device_id', '') or ''
