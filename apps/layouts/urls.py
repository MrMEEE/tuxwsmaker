from django.urls import path

from .views import (
    PartitionEntryCreateView,
    PartitionEntryDeleteView,
    PartitionEntryUpdateView,
    PartitionEntriesBulkUpdateView,
    PartitionLayoutCreateView,
    PartitionLayoutDeleteView,
    PartitionLayoutDetailView,
    PartitionLayoutListView,
    PartitionLayoutUpdateView,
    PartitionYAMLPreviewView,
    PartitionYAMLUpdateView,
)

app_name = "layouts"

urlpatterns = [
    path("", PartitionLayoutListView.as_view(), name="layout-list"),
    path("new/", PartitionLayoutCreateView.as_view(), name="layout-create"),
    path("<int:pk>/", PartitionLayoutDetailView.as_view(), name="layout-detail"),
    path("<int:pk>/edit/", PartitionLayoutUpdateView.as_view(), name="layout-edit"),
    path("<int:pk>/delete/", PartitionLayoutDeleteView.as_view(), name="layout-delete"),
    path("<int:pk>/entries/new/", PartitionEntryCreateView.as_view(), name="entry-create"),
    path("<int:pk>/entries/<int:entry_id>/edit/", PartitionEntryUpdateView.as_view(), name="entry-edit"),
    path("<int:pk>/entries/<int:entry_id>/delete/", PartitionEntryDeleteView.as_view(), name="entry-delete"),
    path("<int:pk>/entries/bulk/", PartitionEntriesBulkUpdateView.as_view(), name="entries-bulk-update"),
    path("<int:pk>/yaml/", PartitionYAMLUpdateView.as_view(), name="layout-yaml"),
    path("<int:pk>/yaml/preview/", PartitionYAMLPreviewView.as_view(), name="layout-yaml-preview"),
]
