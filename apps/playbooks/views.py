from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.views.generic import CreateView, DetailView, ListView, UpdateView, View

from apps.realtime.events import publish_event
from apps.builds.models import SSHKey

from .forms import PlaybookRepositoryForm
from .models import PlaybookRepository
from .services import PlaybookSyncError, inspect_repository, sync_branches, sync_playbooks


class PlaybookRepositoryListView(LoginRequiredMixin, ListView):
    model = PlaybookRepository
    template_name = "playbooks/repo_list.html"
    context_object_name = "items"


def _is_modal_request(request) -> bool:
    return request.GET.get("modal") == "1" or request.POST.get("modal") == "1" or request.headers.get("X-Modal-Request") == "1"


class PlaybookRepositoryCreateView(LoginRequiredMixin, CreateView):
    model = PlaybookRepository
    form_class = PlaybookRepositoryForm
    template_name = "playbooks/repo_form.html"
    success_url = reverse_lazy("playbooks:repo-list")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def get_template_names(self):
        if _is_modal_request(self.request):
            return ["playbooks/repo_form_fragment.html"]
        return [self.template_name]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["modal_mode"] = _is_modal_request(self.request)
        return context

    def form_invalid(self, form):
        if _is_modal_request(self.request):
            return self.render_to_response(self.get_context_data(form=form), status=400)
        return super().form_invalid(form)

    def form_valid(self, form):
        response = super().form_valid(form)
        try:
            sync_branches(self.object)
            sync_playbooks(self.object, self.object.default_branch)
            messages.success(self.request, "Repository saved and scanned for branches/playbooks")
        except PlaybookSyncError as exc:
            messages.warning(self.request, f"Repository saved, but initial sync failed: {exc}")
        publish_event("playbooks", "created", {"repo_id": self.object.id})
        if _is_modal_request(self.request):
            return JsonResponse({"ok": True, "repo_id": self.object.id, "message": "Repository saved"})
        return response


class PlaybookRepositoryUpdateView(LoginRequiredMixin, UpdateView):
    model = PlaybookRepository
    form_class = PlaybookRepositoryForm
    template_name = "playbooks/repo_form.html"
    success_url = reverse_lazy("playbooks:repo-list")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def get_template_names(self):
        if _is_modal_request(self.request):
            return ["playbooks/repo_form_fragment.html"]
        return [self.template_name]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["modal_mode"] = _is_modal_request(self.request)
        return context

    def form_invalid(self, form):
        if _is_modal_request(self.request):
            return self.render_to_response(self.get_context_data(form=form), status=400)
        return super().form_invalid(form)

    def form_valid(self, form):
        response = super().form_valid(form)
        try:
            sync_branches(self.object)
            sync_playbooks(self.object, self.object.default_branch)
            messages.success(self.request, "Repository updated and rescanned")
        except PlaybookSyncError as exc:
            messages.warning(self.request, f"Repository updated, but sync failed: {exc}")
        publish_event("playbooks", "updated", {"repo_id": self.object.id})
        if _is_modal_request(self.request):
            return JsonResponse({"ok": True, "repo_id": self.object.id, "message": "Repository updated"})
        return response


class PlaybookRepositoryDeleteView(LoginRequiredMixin, View):
    def post(self, request, pk):
        obj = get_object_or_404(PlaybookRepository, pk=pk)
        deleted_id = obj.id
        obj.delete()
        publish_event("playbooks", "deleted", {"repo_id": deleted_id})
        messages.success(request, "Playbook repository deleted")
        return redirect("playbooks:repo-list")


class PlaybookRepositoryDetailView(LoginRequiredMixin, DetailView):
    model = PlaybookRepository
    template_name = "playbooks/repo_detail.html"
    context_object_name = "repo"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["initial_tree"] = None
        context["initial_tree_error"] = ""
        try:
            context["initial_tree"] = inspect_repository(
                self.object.repo_url,
                self.object.default_branch,
                ssh_key=self.object.ssh_key if self.object.ssh_key and self.object.ssh_key.scope == SSHKey.SCOPE_USER else None,
                api_key=self.object.get_api_key(),
            )
        except PlaybookSyncError as exc:
            context["initial_tree_error"] = str(exc)
        return context


class PlaybookRepositoryInspectView(LoginRequiredMixin, View):
    def post(self, request):
        repo_url = (request.POST.get("repo_url") or "").strip()
        branch = (request.POST.get("branch") or "").strip() or None
        ssh_key_id = (request.POST.get("ssh_key") or "").strip()
        api_key = request.POST.get("api_key") or ""
        if not repo_url:
            return JsonResponse({"ok": False, "error": "repo_url is required"}, status=400)

        try:
            ssh_key = None
            if ssh_key_id:
                ssh_key = get_object_or_404(SSHKey, pk=ssh_key_id, scope=SSHKey.SCOPE_USER, owner=request.user)
            data = inspect_repository(repo_url=repo_url, preferred_branch=branch, ssh_key=ssh_key, api_key=api_key)
        except PlaybookSyncError as exc:
            return JsonResponse({"ok": False, "error": str(exc)}, status=400)
        return JsonResponse({"ok": True, **data})


class PlaybookRepositorySyncBranchesView(LoginRequiredMixin, View):
    def post(self, request, pk):
        repo = get_object_or_404(PlaybookRepository, pk=pk)
        try:
            branches = sync_branches(repo)
            messages.success(request, f"Found {len(branches)} branch(es)")
            publish_event("playbooks", "branches-synced", {"repo_id": repo.id, "count": len(branches)})
        except PlaybookSyncError as exc:
            messages.error(request, f"Branch sync failed: {exc}")
        return redirect("playbooks:repo-detail", pk=repo.pk)


class PlaybookRepositorySyncPlaybooksView(LoginRequiredMixin, View):
    def post(self, request, pk):
        repo = get_object_or_404(PlaybookRepository, pk=pk)
        branch = (request.POST.get("branch") or repo.default_branch).strip()
        try:
            playbooks = sync_playbooks(repo, branch)
            messages.success(request, f"Found {len(playbooks)} playbook(s) in branch '{branch}'")
            publish_event(
                "playbooks",
                "playbooks-synced",
                {"repo_id": repo.id, "count": len(playbooks), "branch": branch},
            )
        except PlaybookSyncError as exc:
            messages.error(request, f"Playbook sync failed: {exc}")
        return redirect("playbooks:repo-detail", pk=repo.pk)
