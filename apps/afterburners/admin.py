from django.contrib import admin

from .models import AfterburnerItem, AfterburnerProfile, AfterburnerScriptInput


@admin.register(AfterburnerProfile)
class AfterburnerProfileAdmin(admin.ModelAdmin):
    list_display = ("name", "updated_at")
    search_fields = ("name",)


@admin.register(AfterburnerItem)
class AfterburnerItemAdmin(admin.ModelAdmin):
    list_display = ("name", "profile", "item_type", "order")
    list_filter = ("item_type", "profile")
    search_fields = ("name", "description")


@admin.register(AfterburnerScriptInput)
class AfterburnerScriptInputAdmin(admin.ModelAdmin):
    list_display = ("key", "item", "input_type", "order", "required")
    list_filter = ("input_type", "required")
    search_fields = ("key", "label")
