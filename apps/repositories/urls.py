from django.urls import path

from .views import (
    RHSMRepositorySyncView,
    PackageRepositoryCreateView,
    PackageRepositoryDeleteView,
    PackageRepositoryDetailView,
    PackageRepositoryListView,
    PackageRepositoryUpdateView,
)

app_name = "repositories"

urlpatterns = [
    path("", PackageRepositoryListView.as_view(), name="repo-list"),
    path("rhsm/sync/", RHSMRepositorySyncView.as_view(), name="rhsm-sync"),
    path("new/", PackageRepositoryCreateView.as_view(), name="repo-create"),
    path("<int:pk>/", PackageRepositoryDetailView.as_view(), name="repo-detail"),
    path("<int:pk>/edit/", PackageRepositoryUpdateView.as_view(), name="repo-edit"),
    path("<int:pk>/delete/", PackageRepositoryDeleteView.as_view(), name="repo-delete"),
]