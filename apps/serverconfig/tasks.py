from __future__ import annotations

from pathlib import Path

from celery import shared_task
from django.core.cache import cache

from apps.builds.services.artifacts import prepare_iso_pxe_assets
from apps.builds.services.builder import BuilderError, BuilderVMManager
from apps.catalog.models import ISOImage
from apps.realtime.events import publish_event
from apps.serverconfig.models import BuilderProgressEvent, ServerConfiguration

BUILDER_PROGRESS_LIMIT = 300
BUILDER_JOB_LOCK_KEY = "serverconfig:builder-job-running"
BUILDER_ACTIVE_TASK_KEY = "serverconfig:builder-active-task-id"
RUN_BUILDER_SETUP_TASK = "apps.serverconfig.tasks.run_builder_setup"


def _append_builder_progress(*, stage: str, message: str, run_id: str = "") -> BuilderProgressEvent:
    event = BuilderProgressEvent.objects.create(stage=stage, message=message, run_id=run_id)
    old_ids = list(
        BuilderProgressEvent.objects.order_by("-created_at", "-id").values_list("id", flat=True)[
            BUILDER_PROGRESS_LIMIT:
        ]
    )
    if old_ids:
        BuilderProgressEvent.objects.filter(id__in=old_ids).delete()
    return event


def _emit_progress(*, stage: str, message: str, run_id: str = "") -> None:
    event = _append_builder_progress(stage=stage, message=message, run_id=run_id)
    publish_event(
        "serverconfig",
        "builder-progress",
        {
            "stage": stage,
            "message": message,
            "ts": event.created_at.isoformat(),
            "run_id": run_id,
        },
    )


def _is_current_task(task_id: str) -> bool:
    if not task_id:
        return False
    active_task_id = cache.get(BUILDER_ACTIVE_TASK_KEY)
    # If the active marker is absent, do not self-cancel on uncertainty.
    if not active_task_id:
        return True
    return active_task_id == task_id


def _cancel_if_superseded(*, task_id: str, run_id: str) -> bool:
    if _is_current_task(task_id):
        return False
    _emit_progress(
        stage="error",
        message="Builder setup cancelled because a newer request was queued",
        run_id=run_id,
    )
    return True


@shared_task(bind=True)
def run_builder_setup(
    self,
    *,
    action: str,
    rhn_username: str,
    rhn_password: str,
    use_redhat_subscription: bool = True,
    run_id: str = "",
) -> dict[str, str | int]:
    task_id = str(getattr(self.request, "id", "") or "")
    if _cancel_if_superseded(task_id=task_id, run_id=run_id):
        return {"status": "cancelled"}

    if not cache.add(BUILDER_JOB_LOCK_KEY, task_id or "1", timeout=2 * 60 * 60):
        if _cancel_if_superseded(task_id=task_id, run_id=run_id):
            return {"status": "cancelled"}
        _emit_progress(stage="error", message="Another builder setup task is already running", run_id=run_id)
        return {"status": "busy"}

    shared_iso_count = 0
    builder_ip = ""
    try:
        builder_manager = BuilderVMManager()
        _emit_progress(stage="start", message="Starting builder creation flow", run_id=run_id)
        if _cancel_if_superseded(task_id=task_id, run_id=run_id):
            return {"status": "cancelled"}

        builder_manager.ensure_access_keypair(force_new=action == "recreate_builder_vm")
        if _cancel_if_superseded(task_id=task_id, run_id=run_id):
            return {"status": "cancelled"}
        for iso_obj in ISOImage.objects.filter(is_active=True).iterator():
            try:
                iso_path = Path(iso_obj.iso_file.path)
            except Exception:
                continue
            if not iso_path.exists():
                continue
            builder_manager.ensure_iso_shared(iso_path)
            prepare_iso_pxe_assets(iso_path=iso_path)
            shared_iso_count += 1

        if _cancel_if_superseded(task_id=task_id, run_id=run_id):
            return {"status": "cancelled"}

        _emit_progress(
            stage="iso",
            message=f"Validated {shared_iso_count} active ISO entries for host mapping",
            run_id=run_id,
        )
        if action == "recreate_builder_vm":
            _emit_progress(
                stage="cleanup",
                message="Recreating builder VM: shutting down and removing old disk",
                run_id=run_id,
            )
            builder_manager.recreate_builder_vm(
                progress_cb=lambda stage, message: _emit_progress(stage=stage, message=message, run_id=run_id)
            )
        else:
            builder_manager.ensure_builder_vm(
                progress_cb=lambda stage, message: _emit_progress(stage=stage, message=message, run_id=run_id)
            )

        if _cancel_if_superseded(task_id=task_id, run_id=run_id):
            return {"status": "cancelled"}

        builder_ip = builder_manager.provision_builder_vm(
            rhn_username=rhn_username,
            rhn_password=rhn_password,
            use_redhat_subscription=use_redhat_subscription,
            progress_cb=lambda stage, message: _emit_progress(stage=stage, message=message, run_id=run_id),
        )

        publish_event("serverconfig", "updated", {"config_id": ServerConfiguration.SINGLETON_PK})
        _emit_progress(stage="done", message=f"Builder available at {builder_ip}", run_id=run_id)
        return {
            "status": "done",
            "builder_ip": builder_ip,
            "shared_iso_count": shared_iso_count,
        }
    except BuilderError as exc:
        _emit_progress(stage="error", message=str(exc), run_id=run_id)
        raise RuntimeError(str(exc)) from exc
    finally:
        if cache.get(BUILDER_JOB_LOCK_KEY) == task_id:
            cache.delete(BUILDER_JOB_LOCK_KEY)
        if cache.get(BUILDER_ACTIVE_TASK_KEY) == task_id:
            cache.delete(BUILDER_ACTIVE_TASK_KEY)
