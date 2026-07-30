from django.contrib import admin

from .models import ISOImage, ISOVariable, OSVariable, OperatingSystem


admin.site.register(OperatingSystem)
admin.site.register(OSVariable)
admin.site.register(ISOImage)
admin.site.register(ISOVariable)

# Register your models here.
