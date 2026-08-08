from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from django.core.cache import cache

from apps.builds.models import BuildDefinition, BuildLogEntry
from apps.realtime.events import publish_event
from apps.serverconfig.models import ServerConfiguration
from apps.workers.tasks import build_task_cache_key, run_build_definition


class HealthView(APIView):
	authentication_classes = []
	permission_classes = []

	def get(self, request):
		return Response({"status": "ok"})


class QueueBuildView(APIView):
	permission_classes = [IsAuthenticated]

	def post(self, request, build_id: int):
		try:
			build = BuildDefinition.objects.get(pk=build_id)
		except BuildDefinition.DoesNotExist:
			return Response({"detail": "Build not found"}, status=status.HTTP_404_NOT_FOUND)

		if build.status in {BuildDefinition.STATUS_QUEUED, BuildDefinition.STATUS_RUNNING}:
			return Response({"detail": "Build is already queued or running"}, status=status.HTTP_409_CONFLICT)

		active_count = BuildDefinition.objects.filter(
			status__in=[BuildDefinition.STATUS_QUEUED, BuildDefinition.STATUS_RUNNING]
		).count()
		limit = ServerConfiguration.get_concurrency_limit()
		if active_count >= limit:
			return Response(
				{"detail": f"Concurrency limit reached ({limit})"},
				status=status.HTTP_429_TOO_MANY_REQUESTS,
			)

		build.status = BuildDefinition.STATUS_QUEUED
		build.started_by = request.user
		build.save(update_fields=["status", "started_by", "updated_at"])
		publish_event("builds", "queued", {"build_id": build.id, "status": build.status})
		BuildLogEntry.objects.create(build=build, stage="queued", message="Build queued")

		task = run_build_definition.delay(build_id)
		cache.set(build_task_cache_key(build.id), task.id, timeout=6 * 60 * 60)
		return Response(
			{
				"task_id": task.id,
				"build_id": build.id,
				"status": build.status,
			},
			status=status.HTTP_202_ACCEPTED,
		)


class BuildsSnapshotView(APIView):
	permission_classes = [IsAuthenticated]

	def get(self, request):
		builds = BuildDefinition.objects.select_related("operating_system").all().order_by("-updated_at")
		data = [
			{
				"id": build.id,
				"name": build.name,
				"operating_system": str(build.operating_system),
				"status": build.status,
				"status_display": build.get_status_display(),
				"output_pxe": build.output_pxe,
				"output_usb_img": build.output_usb_img,
			}
			for build in builds
		]
		return Response({"items": data, "total": len(data)})


class BuildSnapshotView(APIView):
	permission_classes = [IsAuthenticated]

	def get(self, request, build_id: int):
		try:
			build = BuildDefinition.objects.select_related(
				"machine_config", "iso_image", "operating_system", "partition_layout"
			).get(pk=build_id)
		except BuildDefinition.DoesNotExist:
			return Response({"detail": "Build not found"}, status=status.HTTP_404_NOT_FOUND)

		artifacts = [
			{
				"id": artifact.id,
				"artifact_type": artifact.artifact_type,
				"artifact_type_display": artifact.get_artifact_type_display(),
				"file_path": artifact.file_path,
				"sha256": artifact.sha256,
				"created_at": artifact.created_at.isoformat(),
				"download_url": f"/builds/{build.id}/artifacts/{artifact.id}/download/",
			}
			for artifact in build.artifacts.all()
		]
		logs = [
			{
				"stage": log.stage,
				"message": log.message,
				"created_at": log.created_at.isoformat(),
			}
			for log in build.logs.all().order_by("-created_at", "-id")
		]
		step_labels = dict(BuildDefinition.STEP_CHOICES)
		manual_steps = []
		for slug in BuildDefinition.STEP_SEQUENCE:
			manual_steps.append(
				{
					"slug": slug,
					"label": step_labels.get(slug, slug),
					"enabled": build.can_run_manual_step(slug),
					"completed": build.has_completed_step(slug),
					"is_next": build.next_manual_step() == slug,
				}
			)

		return Response(
			{
				"id": build.id,
				"name": build.name,
				"status": build.status,
				"status_display": build.get_status_display(),
				"run_mode": build.run_mode,
				"run_mode_display": build.get_run_mode_display(),
				"current_step": build.current_step,
				"current_step_display": build.get_current_step_display(),
				"manual_steps": manual_steps,
				"artifacts": artifacts,
				"logs": logs,
			}
		)


class DashboardSummaryView(APIView):
	permission_classes = [IsAuthenticated]

	def get(self, request):
		counts = {
			"build_total": BuildDefinition.objects.count(),
			"build_running": BuildDefinition.objects.filter(status=BuildDefinition.STATUS_RUNNING).count(),
			"build_queued": BuildDefinition.objects.filter(status=BuildDefinition.STATUS_QUEUED).count(),
			"build_failed": BuildDefinition.objects.filter(status=BuildDefinition.STATUS_FAILED).count(),
			"build_succeeded": BuildDefinition.objects.filter(status=BuildDefinition.STATUS_SUCCEEDED).count(),
		}
		return Response(counts)

# Create your views here.
