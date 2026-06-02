"""Template views that render the web-client HTML pages.
Each view is gated by
LoginRequiredMixin so an unauthenticated visitor is redirected to /login/.
"""
from __future__ import annotations

from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import LoginView
from django.core.cache import cache
from django.shortcuts import get_object_or_404
from django.views.generic import TemplateView

from .models import Device


class ThrottledLoginView(LoginView):
    """LoginView with per-IP brute-force throttling.
    """

    MAX_ATTEMPTS = 5
    LOCKOUT_SECONDS = 5 * 60

    @staticmethod
    def _client_ip(request) -> str:
        return request.META.get('REMOTE_ADDR') or 'unknown'

    def _cache_key(self, request) -> str:
        return f'login-throttle:{self._client_ip(request)}'

    def post(self, request, *args, **kwargs):
        if cache.get(self._cache_key(request), 0) >= self.MAX_ATTEMPTS:
            form = self.get_form()
            form.add_error(None, 'Забагато невдалих спроб входу. Спробуйте за кілька хвилин.')
            return self.form_invalid(form)
        return super().post(request, *args, **kwargs)

    def form_invalid(self, form):
        key = self._cache_key(self.request)
        cache.set(key, cache.get(key, 0) + 1, self.LOCKOUT_SECONDS)
        return super().form_invalid(form)

    def form_valid(self, form):
        cache.delete(self._cache_key(self.request))
        return super().form_valid(form)


class DashboardView(LoginRequiredMixin, TemplateView):
    template_name = 'dashboard.html'


class DevicesView(LoginRequiredMixin, TemplateView):
    template_name = 'devices.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['priority_choices'] = range(Device.PRIORITY_MIN, Device.PRIORITY_MAX + 1)
        return ctx

class DeviceDetailView(LoginRequiredMixin, TemplateView):
    """Per-device page: live numbers + history graph + state-change timeline."""

    template_name = 'device_detail.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        device = get_object_or_404(Device, pk=kwargs['pk'])
        ctx['device'] = device
        ctx['priority_choices'] = range(Device.PRIORITY_MIN, Device.PRIORITY_MAX + 1)
        return ctx


class SettingsView(LoginRequiredMixin, TemplateView):
    template_name = 'settings.html'


class AccountView(LoginRequiredMixin, TemplateView):
    """Cabinet page: per-period and lifetime consumption + tariff editor."""

    template_name = 'account.html'


class ForecastView(LoginRequiredMixin, TemplateView):
    """Forecast page: projected system consumption with three transparent methods."""

    template_name = 'forecast.html'