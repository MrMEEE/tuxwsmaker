from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.cache import cache
from django.db import IntegrityError
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.views.generic import TemplateView
from config.celery import app as celery_app

from apps.catalog.models import ISOImage, OperatingSystem
from apps.builds.services.artifacts import prepare_iso_pxe_assets
from apps.builds.services.builder import BuilderError, BuilderVMManager
from apps.realtime.events import publish_event
from apps.serverconfig.tasks import BUILDER_ACTIVE_TASK_KEY, BUILDER_JOB_LOCK_KEY, RUN_BUILDER_SETUP_TASK

from .forms import ServerConfigurationForm
from .models import BuilderProgressEvent, ServerConfiguration
from .services.redhat import RedHatDownloadClient, RedHatDownloadError


class ServerConfigurationView(LoginRequiredMixin, TemplateView):
    template_name = "serverconfig/config_form.html"
    LIBVIRT_IMAGE_DIR = Path("/var/lib/libvirt/images")
    BUILDER_IMAGE_DIR = Path(settings.ARTIFACT_ROOT) / "disks"
    BUILDER_PROGRESS_LIMIT = 300

    def _get_builder_progress_history(self) -> list[dict[str, str]]:
        entries = list(
            BuilderProgressEvent.objects.order_by("-created_at", "-id")[: self.BUILDER_PROGRESS_LIMIT]
        )
        entries.reverse()
        return [
            {
                "stage": entry.stage,
                "message": entry.message,
                "ts": entry.created_at.isoformat(),
                "run_id": entry.run_id,
            }
            for entry in entries
        ]

    def _clear_builder_progress_history(self) -> None:
        BuilderProgressEvent.objects.all().delete()

    def _append_builder_progress(self, *, stage: str, message: str, run_id: str = "") -> BuilderProgressEvent:
        event = BuilderProgressEvent.objects.create(stage=stage, message=message, run_id=run_id)
        old_ids = list(
            BuilderProgressEvent.objects.order_by("-created_at", "-id").values_list("id", flat=True)[
                self.BUILDER_PROGRESS_LIMIT :
            ]
        )
        if old_ids:
            BuilderProgressEvent.objects.filter(id__in=old_ids).delete()
        return event

    def _get_catalog_items(self) -> list[dict[str, str]]:
        return self.request.session.get("rhel_qcow2_catalog", [])

    def _store_catalog_items(self, items: list[dict[str, str]]) -> None:
        self.request.session["rhel_qcow2_catalog"] = items
        self.request.session.modified = True

    def _get_iso_catalog_items(self) -> list[dict[str, str]]:
        return self.request.session.get("rhel_iso_catalog", [])

    def _store_iso_catalog_items(self, items: list[dict[str, str]]) -> None:
        self.request.session["rhel_iso_catalog"] = items
        self.request.session.modified = True

    def _store_catalog_debug(self, debug_data: dict[str, str | int | bool]) -> None:
        self.request.session["rhel_qcow2_debug"] = debug_data
        self.request.session.modified = True

    def _get_catalog_debug(self) -> dict[str, str | int | bool]:
        return self.request.session.get("rhel_qcow2_debug", {})

    def _get_available_builder_images(self, selected_path: str = "") -> list[dict[str, str]]:
        image_dir = self.BUILDER_IMAGE_DIR
        images: list[dict[str, str]] = []
        if image_dir.exists():
            for path in sorted(image_dir.iterdir(), key=lambda item: item.name.lower()):
                if not path.is_file() or path.suffix.lower() != ".qcow2":
                    continue
                images.append(
                    {
                        "path": str(path),
                        "label": path.name,
                    }
                )

        selected = selected_path.strip()
        if selected and all(item["path"] != selected for item in images):
            images.insert(
                0,
                {
                    "path": selected,
                    "label": f"{Path(selected).name} (saved selection)",
                },
            )
        return images

    def _config_form(self, cfg: ServerConfiguration, post_data=None):
        if post_data is not None:
            return ServerConfigurationForm(post_data, instance=cfg)
        return ServerConfigurationForm(instance=cfg)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        cfg = ServerConfiguration.get_solo()
        builder_manager = BuilderVMManager()
        try:
            builder_vm_exists = builder_manager.builder_vm_exists()
        except BuilderError:
            builder_vm_exists = False
        context["form"] = kwargs.get("form") or self._config_form(cfg)
        context["available_images"] = self._get_catalog_items()
        context["available_iso_images"] = self._get_iso_catalog_items()
        context["available_builder_images"] = self._get_available_builder_images(
            selected_path=cfg.builder_base_image_path
        )
        context["builder_vm_exists"] = builder_vm_exists
        context["builder_private_key_available"] = cfg.has_builder_ssh_keypair()
        context["builder_progress_history"] = self._get_builder_progress_history()
        context["builder_job_running"] = bool(cache.get(BUILDER_ACTIVE_TASK_KEY))
        context["operating_systems"] = OperatingSystem.objects.order_by("name")
        context["rhel_debug"] = self._get_catalog_debug()
        context["config"] = cfg
        return context

    def get(self, request, *args, **kwargs):
        return self.render_to_response(self.get_context_data())

    def post(self, request, *args, **kwargs):
        cfg = ServerConfiguration.get_solo()
        action = request.POST.get("action", "save")
        wants_json = request.headers.get("x-requested-with") == "XMLHttpRequest"

        if action == "download_builder_private_key":
            builder_manager = BuilderVMManager()
            if not builder_manager.builder_vm_exists():
                messages.error(request, "The builder VM must exist before downloading its private key")
                return redirect("serverconfig:server-config")
            private_key = cfg.get_builder_ssh_private_key()
            if not private_key:
                messages.error(request, "No builder private key is stored yet")
                return redirect("serverconfig:server-config")
            response = HttpResponse(private_key + "\n", content_type="text/plain")
            response["Content-Disposition"] = 'attachment; filename="builder-vm-private-key.txt"'
            return response

        if action in {"save", "create_builder_vm", "recreate_builder_vm"}:
            selected_builder_image_path = (request.POST.get("selected_builder_image_path") or "").strip()
            if selected_builder_image_path:
                available_paths = {
                    item["path"]
                    for item in self._get_available_builder_images(
                        selected_path=cfg.builder_base_image_path
                    )
                }
                if selected_builder_image_path not in available_paths:
                    messages.error(request, "Selected builder base image is not available")
                    return self.render_to_response(self.get_context_data(form=self._config_form(cfg, request.POST)))
                cfg.builder_base_image_path = selected_builder_image_path
                cfg.builder_image_label = Path(selected_builder_image_path).name
                cfg.builder_image_source_url = selected_builder_image_path
            else:
                cfg.builder_base_image_path = ""
                cfg.builder_image_label = ""
                cfg.builder_image_source_url = ""
            form = self._config_form(cfg, request.POST)
            if form.is_valid():
                form.save()
                if action in {"create_builder_vm", "recreate_builder_vm"}:
                    run_id = uuid4().hex
                    task_id = uuid4().hex
                    rhn_username = (request.POST.get("rhn_username") or "").strip()
                    rhn_password = request.POST.get("rhn_password") or ""
                    use_redhat_subscription = bool(cfg.use_redhat_subscription)
                    if use_redhat_subscription and (not rhn_username or not rhn_password):
                        message = "Red Hat username and password are required to create and register the builder VM"
                        event = self._append_builder_progress(stage="error", message=message, run_id=run_id)
                        publish_event(
                            "serverconfig",
                            "builder-progress",
                            {"stage": "error", "message": message, "ts": event.created_at.isoformat(), "run_id": run_id},
                        )
                        if wants_json:
                            return JsonResponse({"ok": False, "error": message}, status=400)
                        messages.error(request, message)
                        return self.render_to_response(self.get_context_data(form=form))

                    prior_task_id = cache.get(BUILDER_ACTIVE_TASK_KEY)
                    if prior_task_id:
                        celery_app.control.revoke(prior_task_id, terminate=True, signal="SIGTERM")
                        cache.delete(BUILDER_JOB_LOCK_KEY)
                        event = self._append_builder_progress(
                            stage="progress",
                            message=f"Superseding in-flight builder task {prior_task_id}",
                            run_id=run_id,
                        )
                        publish_event(
                            "serverconfig",
                            "builder-progress",
                            {
                                "stage": "progress",
                                "message": f"Superseding in-flight builder task {prior_task_id}",
                                "ts": event.created_at.isoformat(),
                                "run_id": run_id,
                            },
                        )

                    self._clear_builder_progress_history()
                    event = self._append_builder_progress(
                        stage="queued", message="Builder setup task queued", run_id=run_id
                    )
                    publish_event(
                        "serverconfig",
                        "builder-progress",
                        {
                            "stage": "queued",
                            "message": "Builder setup task queued",
                            "ts": event.created_at.isoformat(),
                            "run_id": run_id,
                        },
                    )

                    cache.set(BUILDER_ACTIVE_TASK_KEY, task_id, timeout=2 * 60 * 60)
                    try:
                        task = celery_app.send_task(
                            RUN_BUILDER_SETUP_TASK,
                            kwargs={
                                "action": action,
                                "rhn_username": rhn_username,
                                "rhn_password": rhn_password,
                                "use_redhat_subscription": use_redhat_subscription,
                                "run_id": run_id,
                            },
                            task_id=task_id,
                        )
                    except Exception:
                        if cache.get(BUILDER_ACTIVE_TASK_KEY) == task_id:
                            cache.delete(BUILDER_ACTIVE_TASK_KEY)
                        raise
                    queued_message = "Builder setup queued and running in background"
                    if wants_json:
                        return JsonResponse(
                            {
                                "ok": True,
                                "queued": True,
                                "task_id": task_id,
                                "message": queued_message,
                            }
                        )
                    messages.success(request, queued_message)
                    return redirect("serverconfig:server-config")

                publish_event("serverconfig", "updated", {"config_id": cfg.pk})
                messages.success(request, "Server configuration saved")
                return redirect("serverconfig:server-config")
            return self.render_to_response(self.get_context_data(form=form))

        if action in {"list_images", "list_iso_images", "list_iso_versions", "list_iso_page"}:
            username = (request.POST.get("rhn_username") or "").strip()
            password = request.POST.get("rhn_password") or ""
            if not username or not password:
                if wants_json:
                    return JsonResponse({"ok": False, "error": "Red Hat username and password are required for this action"}, status=400)
                messages.error(request, "Red Hat username and password are required for this action")
                return self.render_to_response(self.get_context_data(form=self._config_form(cfg, request.POST)))

            client = RedHatDownloadClient(username=username, password=password)
            if action == "list_images":
                try:
                    images, iso_images = client.list_rhel_images()
                except (RedHatDownloadError, Exception) as exc:
                    if wants_json:
                        return JsonResponse({"ok": False, "error": f"Failed to list Red Hat images: {exc}"}, status=400)
                    messages.error(request, f"Failed to list Red Hat images: {exc}")
                    return self.render_to_response(self.get_context_data(form=self._config_form(cfg, request.POST)))

                catalog = [
                    {"label": image.label, "url": image.url, "major_version": image.major_version}
                    for image in images
                ]
                iso_catalog = [
                    {"label": item.label, "url": item.url, "version": item.version, "major_version": item.major_version}
                    for item in iso_images
                ]
                self._store_catalog_items(catalog)
                self._store_iso_catalog_items(iso_catalog)
                self._store_catalog_debug(client.last_debug)
                if catalog or iso_catalog:
                    message = f"Loaded {len(catalog)} qcow2 image entries and {len(iso_catalog)} ISO image entries from Red Hat"
                    if wants_json:
                        return JsonResponse(
                            {
                                "ok": True,
                                "action": "list_images",
                                "message": message,
                                "qcow2_catalog": catalog,
                                "iso_catalog": iso_catalog,
                                "debug": client.last_debug,
                            }
                        )
                    messages.success(request, message)
                else:
                    if wants_json:
                        return JsonResponse(
                            {
                                "ok": True,
                                "action": "list_images",
                                "message": "No qcow2 or ISO images found on the Red Hat downloads page",
                                "qcow2_catalog": catalog,
                                "iso_catalog": iso_catalog,
                                "debug": client.last_debug,
                            }
                        )
                    messages.warning(request, "No qcow2 or ISO images found on the Red Hat downloads page")
            elif action == "list_iso_images":
                try:
                    iso_images = client.list_rhel_iso_images()
                except (RedHatDownloadError, Exception) as exc:
                    if wants_json:
                        return JsonResponse({"ok": False, "error": f"Failed to list Red Hat ISO images: {exc}"}, status=400)
                    messages.error(request, f"Failed to list Red Hat ISO images: {exc}")
                    return self.render_to_response(self.get_context_data(form=self._config_form(cfg, request.POST)))

                iso_catalog = [
                    {"label": item.label, "url": item.url, "version": item.version, "major_version": item.major_version}
                    for item in iso_images
                ]
                self._store_iso_catalog_items(iso_catalog)
                self._store_catalog_debug(client.last_debug)
                if iso_catalog:
                    if wants_json:
                        return JsonResponse(
                            {
                                "ok": True,
                                "action": "list_iso_images",
                                "message": f"Loaded {len(iso_catalog)} ISO image entries from Red Hat",
                                "qcow2_catalog": self._get_catalog_items(),
                                "iso_catalog": iso_catalog,
                                "debug": client.last_debug,
                            }
                        )
                    messages.success(request, f"Loaded {len(iso_catalog)} ISO image entries from Red Hat")
                else:
                    if wants_json:
                        return JsonResponse(
                            {
                                "ok": True,
                                "action": "list_iso_images",
                                "message": "No ISO images found in the Red Hat product-software catalogs",
                                "qcow2_catalog": self._get_catalog_items(),
                                "iso_catalog": iso_catalog,
                                "debug": client.last_debug,
                            }
                        )
                    messages.warning(request, "No ISO images found in the Red Hat product-software catalogs")
            elif action == "list_iso_versions":
                try:
                    pages = client.list_rhel_iso_version_pages()
                except (RedHatDownloadError, Exception) as exc:
                    if wants_json:
                        return JsonResponse({"ok": False, "error": f"Failed to enumerate Red Hat version pages: {exc}"}, status=400)
                    messages.error(request, f"Failed to enumerate Red Hat version pages: {exc}")
                    return self.render_to_response(self.get_context_data(form=self._config_form(cfg, request.POST)))

                # Clear previous ISO catalog for fresh sequential loading.
                self._store_iso_catalog_items([])
                self._store_catalog_debug(client.last_debug)
                if wants_json:
                    return JsonResponse(
                        {
                            "ok": True,
                            "action": "list_iso_versions",
                            "message": f"Found {len(pages)} Red Hat version pages",
                            "iso_pages": pages,
                            "debug": client.last_debug,
                        }
                    )
                messages.success(request, f"Found {len(pages)} Red Hat version pages")
            else:  # list_iso_page
                page_url = (request.POST.get("iso_page_url") or "").strip()
                if not page_url:
                    if wants_json:
                        return JsonResponse({"ok": False, "error": "Missing ISO page URL"}, status=400)
                    messages.error(request, "Missing ISO page URL")
                    return self.render_to_response(self.get_context_data(form=self._config_form(cfg, request.POST)))

                try:
                    page_items = client.list_rhel_iso_images_for_version_page(page_url)
                except (RedHatDownloadError, Exception) as exc:
                    if wants_json:
                        return JsonResponse({"ok": False, "error": f"Failed to load ISO page: {exc}"}, status=400)
                    messages.error(request, f"Failed to load ISO page: {exc}")
                    return self.render_to_response(self.get_context_data(form=self._config_form(cfg, request.POST)))

                current_catalog = self._get_iso_catalog_items()
                by_url = {item.get("url"): item for item in current_catalog if item.get("url")}
                for item in page_items:
                    by_url[item.url] = {
                        "label": item.label,
                        "url": item.url,
                        "version": item.version,
                        "major_version": item.major_version,
                    }
                merged_catalog = list(by_url.values())
                merged_catalog.sort(
                    key=lambda entry: (
                        int(str(entry.get("major_version") or 0)),
                        str(entry.get("version") or ""),
                        str(entry.get("label") or ""),
                    ),
                    reverse=True,
                )
                self._store_iso_catalog_items(merged_catalog)
                self._store_catalog_debug(client.last_debug)

                if wants_json:
                    return JsonResponse(
                        {
                            "ok": True,
                            "action": "list_iso_page",
                            "message": f"Loaded {len(page_items)} DVD ISO image entries from this version page",
                            "iso_page_items": [
                                {
                                    "label": item.label,
                                    "url": item.url,
                                    "version": item.version,
                                    "major_version": item.major_version,
                                }
                                for item in page_items
                            ],
                            "iso_catalog": merged_catalog,
                            "debug": client.last_debug,
                        }
                    )
                messages.success(request, f"Loaded {len(page_items)} DVD ISO image entries from this version page")
            return redirect("serverconfig:server-config")

        if action == "download_image":
            username = (request.POST.get("rhn_username") or "").strip()
            password = request.POST.get("rhn_password") or ""
            if not username or not password:
                if wants_json:
                    return JsonResponse({"ok": False, "error": "Red Hat username and password are required for this action"}, status=400)
                messages.error(request, "Red Hat username and password are required for this action")
                return self.render_to_response(self.get_context_data(form=self._config_form(cfg, request.POST)))

            client = RedHatDownloadClient(username=username, password=password)
            image_url = (request.POST.get("selected_image_url") or "").strip()
            if not image_url:
                if wants_json:
                    return JsonResponse({"ok": False, "error": "Select an image before downloading"}, status=400)
                messages.error(request, "Select an image before downloading")
                return self.render_to_response(self.get_context_data(form=self._config_form(cfg, request.POST)))

            catalog = self._get_catalog_items()
            selected = next((item for item in catalog if item.get("url") == image_url), None)
            label = selected.get("label") if selected else image_url
            output_dir = Path(settings.ARTIFACT_ROOT) / "disks"

            try:
                downloaded_path = client.download_image(image_url=image_url, output_dir=output_dir)
            except (RedHatDownloadError, Exception) as exc:
                if wants_json:
                    return JsonResponse({"ok": False, "error": f"Failed to download selected Red Hat image: {exc}"}, status=400)
                messages.error(request, f"Failed to download selected Red Hat image: {exc}")
                return self.render_to_response(self.get_context_data(form=self._config_form(cfg, request.POST)))

            cfg.builder_base_image_path = str(downloaded_path)
            cfg.builder_image_label = label or ""
            cfg.builder_image_source_url = image_url
            cfg.save()
            publish_event("serverconfig", "updated", {"config_id": cfg.pk})
            if wants_json:
                return JsonResponse(
                    {
                        "ok": True,
                        "action": "download_image",
                        "message": f"Downloaded builder base image to {downloaded_path}",
                        "builder": {
                            "path": cfg.builder_base_image_path,
                            "label": cfg.builder_image_label,
                            "source_url": cfg.builder_image_source_url,
                        },
                    }
                )
            messages.success(request, f"Downloaded builder base image to {downloaded_path}")
            return redirect("serverconfig:server-config")

        if action == "download_add_iso":
            username = (request.POST.get("rhn_username") or "").strip()
            password = request.POST.get("rhn_password") or ""
            if not username or not password:
                if wants_json:
                    return JsonResponse({"ok": False, "error": "Red Hat username and password are required for this action"}, status=400)
                messages.error(request, "Red Hat username and password are required for this action")
                return self.render_to_response(self.get_context_data(form=self._config_form(cfg, request.POST)))

            selected_iso_url = (request.POST.get("selected_iso_url") or "").strip()
            selected_os_id = (request.POST.get("selected_iso_os_id") or "").strip()
            if not selected_iso_url:
                if wants_json:
                    return JsonResponse({"ok": False, "error": "Select an ISO image before downloading"}, status=400)
                messages.error(request, "Select an ISO image before downloading")
                return self.render_to_response(self.get_context_data(form=self._config_form(cfg, request.POST)))
            if not selected_os_id:
                if wants_json:
                    return JsonResponse({"ok": False, "error": "Select an operating system to attach the downloaded ISO to"}, status=400)
                messages.error(request, "Select an operating system to attach the downloaded ISO to")
                return self.render_to_response(self.get_context_data(form=self._config_form(cfg, request.POST)))

            os_obj = OperatingSystem.objects.filter(pk=selected_os_id).first()
            if os_obj is None:
                if wants_json:
                    return JsonResponse({"ok": False, "error": "Selected operating system does not exist"}, status=400)
                messages.error(request, "Selected operating system does not exist")
                return self.render_to_response(self.get_context_data(form=self._config_form(cfg, request.POST)))

            client = RedHatDownloadClient(username=username, password=password)
            iso_catalog = self._get_iso_catalog_items()
            selected = next((item for item in iso_catalog if item.get("url") == selected_iso_url), None)
            label = selected.get("label") if selected else selected_iso_url
            version_hint = selected.get("version") if selected else "10"
            major_hint = selected.get("major_version") if selected else "10"

            try:
                downloaded_path = client.download_image(image_url=selected_iso_url, output_dir=Path(settings.ISO_UPLOAD_ROOT))
            except (RedHatDownloadError, Exception) as exc:
                if wants_json:
                    return JsonResponse({"ok": False, "error": f"Failed to download selected Red Hat ISO image: {exc}"}, status=400)
                messages.error(request, f"Failed to download selected Red Hat ISO image: {exc}")
                return self.render_to_response(self.get_context_data(form=self._config_form(cfg, request.POST)))

            media_root = Path(settings.MEDIA_ROOT).resolve()
            try:
                relative_iso_path = downloaded_path.resolve().relative_to(media_root).as_posix()
            except ValueError:
                if wants_json:
                    return JsonResponse({"ok": False, "error": "Downloaded ISO path is outside MEDIA_ROOT and cannot be registered"}, status=400)
                messages.error(request, "Downloaded ISO path is outside MEDIA_ROOT and cannot be registered")
                return self.render_to_response(self.get_context_data(form=self._config_form(cfg, request.POST)))

            stem = downloaded_path.stem
            version_value = f"rhel{major_hint}-{version_hint}-{stem}"[:64]
            os_version = f"{major_hint}.{version_hint}"[:64] if "." not in str(version_hint) else str(version_hint)[:64]
            try:
                iso_obj = ISOImage.objects.create(
                    operating_system=os_obj,
                    version=version_value,
                    os_version=os_version,
                    iso_file=relative_iso_path,
                )
            except IntegrityError:
                if wants_json:
                    return JsonResponse({"ok": False, "error": f"An ISO with version '{version_value}' already exists for {os_obj.name}"}, status=400)
                messages.error(request, f"An ISO with version '{version_value}' already exists for {os_obj.name}")
                return self.render_to_response(self.get_context_data(form=self._config_form(cfg, request.POST)))

            prepare_iso_pxe_assets(iso_path=Path(iso_obj.iso_file.path))

            publish_event("catalog", "iso-uploaded", {"os_id": os_obj.pk, "count": 1})
            if wants_json:
                return JsonResponse(
                    {
                        "ok": True,
                        "action": "download_add_iso",
                        "message": f"Downloaded and added ISO '{label}' to {os_obj.name}",
                        "added_iso": {
                            "os_id": os_obj.pk,
                            "os_name": os_obj.name,
                            "version": iso_obj.version,
                            "os_version": iso_obj.os_version,
                            "path": relative_iso_path,
                        },
                    }
                )
            messages.success(request, f"Downloaded and added ISO '{label}' to {os_obj.name}")
            return redirect("serverconfig:server-config")

        messages.error(request, "Unknown action")
        return redirect("serverconfig:server-config")
