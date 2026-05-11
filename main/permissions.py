"""Custom DRF permission classes."""
from __future__ import annotations

import hmac

from django.conf import settings
from rest_framework.permissions import BasePermission


API_KEY_HEADER = 'HTTP_X_API_KEY'


class HasDeviceApiKey(BasePermission):
    """Allow access only when the request carries the configured X-API-Key header.
    """

    message = 'Invalid or missing X-API-Key header.'

    def has_permission(self, request, view) -> bool:
        expected = getattr(settings, 'DEVICE_API_KEY', '') or ''
        provided = request.META.get(API_KEY_HEADER, '') or ''

        if not expected:
            return False

        return hmac.compare_digest(expected, provided)
