from django.contrib import admin

from .models import Playbook, PlaybookBranch, PlaybookRepository


admin.site.register(PlaybookRepository)
admin.site.register(PlaybookBranch)
admin.site.register(Playbook)
