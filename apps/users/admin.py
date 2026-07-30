from django.contrib import admin

from .models import LDAPGroupMapping, LDAPSource, User, UserRole


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
	list_display = ("username", "email", "is_local", "is_staff", "is_active")
	list_filter = ("is_local", "is_staff", "is_active")
	search_fields = ("username", "email")


@admin.register(UserRole)
class UserRoleAdmin(admin.ModelAdmin):
	list_display = ("user", "role")
	list_filter = ("role",)


@admin.register(LDAPSource)
class LDAPSourceAdmin(admin.ModelAdmin):
	list_display = ("name", "hostname", "port", "protocol", "is_active")
	list_filter = ("protocol", "is_active")


@admin.register(LDAPGroupMapping)
class LDAPGroupMappingAdmin(admin.ModelAdmin):
	list_display = ("source", "ldap_group_dn", "role")

# Register your models here.
