from django.urls import path

from .api_views import posts_list, post_detail


urlpatterns = [
    path('posts/', posts_list),
    path('posts/<slug:slug>/', post_detail),
]
