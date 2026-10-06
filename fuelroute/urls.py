"""URL configuration for fuelroute project."""
from django.contrib import admin
from django.urls import path

from fuelapi import views

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/optimize/', views.optimize, name='optimize'),
    path('demo/', views.demo, name='demo'),
    path('', views.demo, name='home'),
]
