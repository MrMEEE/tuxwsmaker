from __future__ import annotations

import shutil
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.builds.models import BuildArtifact
from apps.serverconfig.models import ServerConfiguration


class Command(BaseCommand):
    help = "Delete expired build artifacts based on server configuration retention days"

    def handle(self, *args, **options):
        cfg = ServerConfiguration.get_effective()
        retention_days = cfg.artifact_retention_days if cfg else 30
        cutoff = timezone.now() - timedelta(days=retention_days)

        artifact_root = Path(settings.ARTIFACT_ROOT).resolve()
        qs = BuildArtifact.objects.filter(created_at__lt=cutoff)

        deleted_files = 0
        deleted_dirs = 0
        deleted_rows = 0

        for artifact in qs.iterator():
            path = Path(artifact.file_path).resolve()
            if artifact_root in path.parents or artifact_root == path:
                if path.is_file():
                    path.unlink(missing_ok=True)
                    deleted_files += 1
                elif path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                    deleted_dirs += 1
            artifact.delete()
            deleted_rows += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"cleanup_artifacts complete: rows={deleted_rows}, files={deleted_files}, dirs={deleted_dirs}, retention_days={retention_days}"
            )
        )
