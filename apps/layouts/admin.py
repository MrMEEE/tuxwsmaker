from django.contrib import admin

from .models import PartitionEntry, PartitionLayout


admin.site.register(PartitionLayout)
admin.site.register(PartitionEntry)

# Register your models here.
