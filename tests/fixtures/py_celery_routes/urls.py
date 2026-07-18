from django.urls import path


def list_users(request):
    pass


urlpatterns = [
    path("users/", list_users),   # Django route -> entry point
]
