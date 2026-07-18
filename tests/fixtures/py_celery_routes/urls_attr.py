from django.urls import path
from . import views

urlpatterns = [
    path("profile/", views.show_profile),   # attribute view -> must resolve (free function)
]
