"""URL routes for the human-facing web client."""
from django.contrib.auth import views as auth_views
from django.urls import path

from . import ui_views


urlpatterns = [
    path('', ui_views.DashboardView.as_view(), name='dashboard'),
    path('devices/', ui_views.DevicesView.as_view(), name='devices-page'),
    path('devices/<int:pk>/', ui_views.DeviceDetailView.as_view(), name='device-detail-page'),
    path('settings/', ui_views.SettingsView.as_view(), name='settings-page'),
    path('account/', ui_views.AccountView.as_view(), name='account-page'),

    path(
        'login/',
        auth_views.LoginView.as_view(
            template_name='registration/login.html',
            redirect_authenticated_user=True,
        ),
        name='login',
    ),
    path(
        'logout/',
        auth_views.LogoutView.as_view(),
        name='logout',
    ),
]