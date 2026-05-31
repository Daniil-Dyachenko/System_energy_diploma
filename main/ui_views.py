"""Template views that render the web-client HTML pages.
Each view is gated by
LoginRequiredMixin so an unauthenticated visitor is redirected to /login/.
"""
from __future__ import annotations

from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404
from django.views.generic import TemplateView

from .models import Device


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