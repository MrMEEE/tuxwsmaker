from django.urls import path

from .views import (
    PlaybookRepositoryCreateView,
    PlaybookRepositoryDeleteView,
    PlaybookRepositoryDetailView,
    PlaybookRepositoryRefreshView,
    PlaybookRepositoryInspectView,
    PlaybookRepositoryListView,
    PlaybookRepositorySyncBranchesView,
    PlaybookRepositorySyncPlaybooksView,
    PlaybookRepositoryUpdateView,
)

app_name = "playbooks"

urlpatterns = [
    path("inspect/", PlaybookRepositoryInspectView.as_view(), name="repo-inspect"),
    path("", PlaybookRepositoryListView.as_view(), name="repo-list"),
    path("new/", PlaybookRepositoryCreateView.as_view(), name="repo-create"),
    path("<int:pk>/", PlaybookRepositoryDetailView.as_view(), name="repo-detail"),
    path("<int:pk>/edit/", PlaybookRepositoryUpdateView.as_view(), name="repo-edit"),
    path("<int:pk>/delete/", PlaybookRepositoryDeleteView.as_view(), name="repo-delete"),
    path("<int:pk>/refresh/", PlaybookRepositoryRefreshView.as_view(), name="repo-refresh"),
    path("<int:pk>/sync-branches/", PlaybookRepositorySyncBranchesView.as_view(), name="repo-sync-branches"),
    path("<int:pk>/sync-playbooks/", PlaybookRepositorySyncPlaybooksView.as_view(), name="repo-sync-playbooks"),
]
