from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.workers.tasks import reconcile_stale_build_states_on_startup


class Command(BaseCommand):
    help = "Reconcile stale queued/running build states after service restarts"

    def handle(self, *args, **options):
        recovered = reconcile_stale_build_states_on_startup()
        self.stdout.write(self.style.SUCCESS(f"Recovered stale build states: {recovered}"))
