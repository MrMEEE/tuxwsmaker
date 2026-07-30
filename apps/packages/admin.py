from django.contrib import admin

from .models import PackageItem, PackageList


admin.site.register(PackageList)
admin.site.register(PackageItem)

# Register your models here.
