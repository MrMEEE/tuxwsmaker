from django.urls import path
from .views import BuildSnapshotView, BuildsSnapshotView, DashboardSummaryView, HealthView, QueueBuildView


urlpatterns = [
    path("health/", HealthView.as_view(), name="health"),
    path("builds/<int:build_id>/queue/", QueueBuildView.as_view(), name="queue-build"),
    path("builds/snapshot/", BuildsSnapshotView.as_view(), name="builds-snapshot"),
    path("builds/<int:build_id>/snapshot/", BuildSnapshotView.as_view(), name="build-snapshot"),
    path("dashboard/summary/", DashboardSummaryView.as_view(), name="dashboard-summary"),
]
