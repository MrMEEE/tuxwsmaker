from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from ldap3 import ALL, Connection, Server

from .models import LDAPSource


class LocalModelBackend(ModelBackend):
    def user_can_authenticate(self, user):
        return super().user_can_authenticate(user) and user.is_local


class LDAPBackend(ModelBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        if not username or not password:
            return None

        user_model = get_user_model()

        for source in LDAPSource.objects.filter(is_active=True):
            server = Server(source.hostname, port=source.port, get_info=ALL, use_ssl=source.protocol == LDAPSource.PROTOCOL_LDAPS)
            try:
                bind_dn = source.bind_dn or None
                bind_password = source.get_bind_password() or None
                with Connection(server, user=bind_dn, password=bind_password, auto_bind=True) as conn:
                    search_filter = f"({source.attr_username}={username})"
                    if not conn.search(source.base_dn, search_filter, attributes=["distinguishedName", source.attr_first_name, source.attr_last_name, source.attr_email]):
                        continue
                    entry = conn.entries[0]
                    user_dn = entry.entry_dn

                with Connection(server, user=user_dn, password=password, auto_bind=True):
                    pass

                defaults = {
                    "first_name": str(getattr(entry, source.attr_first_name, ""))[:150],
                    "last_name": str(getattr(entry, source.attr_last_name, ""))[:150],
                    "email": str(getattr(entry, source.attr_email, ""))[:254],
                    "is_local": False,
                }
                user, _created = user_model.objects.get_or_create(username=username, defaults=defaults)
                if not user.is_local:
                    for key, value in defaults.items():
                        setattr(user, key, value)
                    user.save(update_fields=["first_name", "last_name", "email", "is_local"])
                return user
            except Exception:
                continue
        return None
