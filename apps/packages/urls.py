from django.urls import path

from .views import (
    PackageItemsUpdateView,
    PackageListCreateView,
    PackageListDeleteView,
    PackageListDetailView,
    PackageListUpdateView,
    PackageListView,
)

app_name = "packages"

urlpatterns = [
    path("", PackageListView.as_view(), name="package-list"),
    path("new/", PackageListCreateView.as_view(), name="package-create"),
    path("<int:pk>/", PackageListDetailView.as_view(), name="package-detail"),
    path("<int:pk>/edit/", PackageListUpdateView.as_view(), name="package-edit"),
    path("<int:pk>/delete/", PackageListDeleteView.as_view(), name="package-delete"),
    path("<int:pk>/items/", PackageItemsUpdateView.as_view(), name="package-items"),
]
