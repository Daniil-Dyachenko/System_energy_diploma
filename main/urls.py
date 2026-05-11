"""URL routes for the energy-monitoring REST API."""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register(r'devices', views.DeviceViewSet, basename='device')

urlpatterns = [
    # ESP32-facing (X-API-Key required)
    path('telemetry/', views.TelemetryIngestView.as_view(), name='telemetry-ingest'),
    path('device-state/', views.DeviceStateView.as_view(), name='device-state'),

    # Web
    path('chart-data/', views.ChartDataView.as_view(), name='chart-data'),
    path('settings/', views.SystemSettingsView.as_view(), name='system-settings'),
    path('current-load/', views.CurrentLoadView.as_view(), name='current-load'),

    # CRUD
    path('', include(router.urls)),
]