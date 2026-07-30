from django.contrib import admin

from .models import BuildArtifact, BuildDefinition, BuildLogEntry, BuildMachineConfig, BuildPlaybookSelection


admin.site.register(BuildMachineConfig)
admin.site.register(BuildDefinition)
admin.site.register(BuildArtifact)
admin.site.register(BuildLogEntry)
admin.site.register(BuildPlaybookSelection)

# Register your models here.
