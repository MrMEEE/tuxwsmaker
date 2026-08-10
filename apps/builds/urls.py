from django.urls import path

from .views import (
    BuildConfigCreateView,
    BuildConfigDeleteView,
    BuildConfigListView,
    BuildConfigUpdateView,
    BuildArtifactDownloadView,
    BuildArtifactPathView,
    BuildCreateView,
    BuildCancelView,
    BuildDeleteView,
    BuildDetailView,
    BuildListView,
    BuildQueueView,
    BuildRerunPlaybooksView,
    BuildRunUntilView,
    BuildRunStepView,
    SSHKeyCreateView,
    SSHKeyDownloadView,
    SSHKeyDeleteView,
    SSHKeyListView,
    BuildUpdateView,
)

app_name = "builds"

urlpatterns = [
    path("", BuildListView.as_view(), name="build-list"),
    path("new/", BuildCreateView.as_view(), name="build-create"),
    path("<int:pk>/", BuildDetailView.as_view(), name="build-detail"),
    path("<int:pk>/edit/", BuildUpdateView.as_view(), name="build-edit"),
    path("<int:pk>/delete/", BuildDeleteView.as_view(), name="build-delete"),
    path("<int:pk>/queue/", BuildQueueView.as_view(), name="build-queue"),
    path("<int:pk>/run-until/", BuildRunUntilView.as_view(), name="build-run-until"),
    path("<int:pk>/steps/<str:step>/", BuildRunStepView.as_view(), name="build-run-step"),
    path("<int:pk>/rerun-playbooks/", BuildRerunPlaybooksView.as_view(), name="build-rerun-playbooks"),
    path("<int:pk>/cancel/", BuildCancelView.as_view(), name="build-cancel"),
    path("<int:pk>/artifacts/<int:artifact_id>/download/", BuildArtifactDownloadView.as_view(), name="artifact-download"),
    path("<int:pk>/artifacts/<int:artifact_id>/path/", BuildArtifactPathView.as_view(), name="artifact-path"),
    path("ssh-keys/", SSHKeyListView.as_view(), name="ssh-key-list"),
    path("ssh-keys/new/", SSHKeyCreateView.as_view(), name="ssh-key-create"),
    path("ssh-keys/<int:pk>/<str:key_type>/download/", SSHKeyDownloadView.as_view(), name="ssh-key-download"),
    path("ssh-keys/<int:pk>/delete/", SSHKeyDeleteView.as_view(), name="ssh-key-delete"),
    path("configs/", BuildConfigListView.as_view(), name="build-config-list"),
    path("configs/new/", BuildConfigCreateView.as_view(), name="build-config-create"),
    path("configs/<int:pk>/edit/", BuildConfigUpdateView.as_view(), name="build-config-edit"),
    path("configs/<int:pk>/delete/", BuildConfigDeleteView.as_view(), name="build-config-delete"),
]
