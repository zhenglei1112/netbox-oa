"""Small NetBox API adapters for real DRF HTTP tests without a NetBox install.

These preserve the POST add/change mapping and token write checks used by the
plugin. Actual NetBox permission backends still need deployment integration QA.
"""

import sys
import types

from rest_framework import serializers, viewsets
from rest_framework.permissions import DjangoObjectPermissions
from rest_framework.routers import DefaultRouter


class TestTokenPermissions(DjangoObjectPermissions):
    perms_map = {
        "GET": ["%(app_label)s.view_%(model_name)s"],
        "OPTIONS": [],
        "HEAD": ["%(app_label)s.view_%(model_name)s"],
        "POST": ["%(app_label)s.add_%(model_name)s"],
        "PUT": ["%(app_label)s.change_%(model_name)s"],
        "PATCH": ["%(app_label)s.change_%(model_name)s"],
        "DELETE": ["%(app_label)s.delete_%(model_name)s"],
    }

    def has_permission(self, request, view):
        if request.method not in {"GET", "HEAD", "OPTIONS"} and request.auth is not None:
            if not getattr(request.auth, "write_enabled", True):
                return False
        return super().has_permission(request, view)

    def has_object_permission(self, request, view, obj):
        # Object constraints are enforced by the adapter's restricted queryset.
        return self.has_permission(request, view)


class TestValidatedSerializer(serializers.ModelSerializer):
    url = serializers.SerializerMethodField()
    display = serializers.SerializerMethodField()

    def __init__(self, *args, fields=None, nested=False, **kwargs):
        super().__init__(*args, **kwargs)
        if fields:
            for key in list(self.fields):
                if key not in fields:
                    self.fields.pop(key)

    def get_url(self, obj):
        return f"/api/test/{obj._meta.model_name}/{obj.pk}/"

    def get_display(self, obj):
        return str(obj)


class TestViewMixin:
    permission_classes = [TestTokenPermissions]

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        mapping = {
            "GET": "view",
            "HEAD": "view",
            "POST": "add",
            "PUT": "change",
            "PATCH": "change",
            "DELETE": "delete",
        }
        if request.method in mapping:
            self.queryset = self.queryset.restrict(request.user, mapping[request.method])


class TestModelViewSet(TestViewMixin, viewsets.ModelViewSet):
    pass


class TestReadOnlyViewSet(TestViewMixin, viewsets.ReadOnlyModelViewSet):
    pass


def install():
    modules = {
        "netbox.api": {},
        "netbox.api.authentication": {"TokenPermissions": TestTokenPermissions},
        "netbox.api.serializers": {"ValidatedModelSerializer": TestValidatedSerializer},
        "netbox.api.viewsets": {
            "NetBoxModelViewSet": TestModelViewSet,
            "NetBoxReadOnlyModelViewSet": TestReadOnlyViewSet,
        },
        "netbox.api.routers": {"NetBoxRouter": DefaultRouter},
    }
    for name, attributes in modules.items():
        module = types.ModuleType(name)
        module.__dict__.update(attributes)
        sys.modules[name] = module
