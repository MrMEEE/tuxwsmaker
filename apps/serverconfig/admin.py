from django.contrib import admin

from .models import BuilderProgressEvent, ServerConfiguration


admin.site.register(ServerConfiguration)


@admin.register(BuilderProgressEvent)
class BuilderProgressEventAdmin(admin.ModelAdmin):
	list_display = ("created_at", "run_id", "stage", "message")
	list_filter = ("stage",)
	search_fields = ("run_id", "message")
 

# Register your models here.
