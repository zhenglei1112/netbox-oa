"""Standalone domain test harness, not a substitute for NetBox integration tests.

Only the NetBox plugin config, base model and users module are replaced. Django,
FSM, FSM logging, database migrations and workflow code remain real.
"""

import sys
import types

from django.apps import AppConfig

SECRET_KEY = "local-tests-only"
INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "tests.support.users.apps.UsersConfig",
    "django_fsm_log",
    "netbox_oa",
]
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
AUTH_USER_MODEL = "users.User"
AUTHENTICATION_BACKENDS = ["tests.support.backends.TestPermissionBackend"]
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True

netbox = types.ModuleType("netbox")
plugins = types.ModuleType("netbox.plugins")
plugins.PluginConfig = AppConfig
sys.modules["netbox"] = netbox
sys.modules["netbox.plugins"] = plugins

# Create the base model lazily: model classes require Django's app registry.
base = types.ModuleType("netbox.models")


def _getattr(name):
    if name != "ChangeLoggedModel":
        raise AttributeError(name)
    from django.db import models

    class RestrictedQuerySet(models.QuerySet):
        def restrict(self, user, action):
            permission = f"{self.model._meta.app_label}.{action}_{self.model._meta.model_name}"
            return self if user.has_perm(permission) else self.none()

    class ChangeLoggedModel(models.Model):
        created = models.DateTimeField(
            auto_now_add=True, blank=True, null=True, verbose_name="created"
        )
        last_updated = models.DateTimeField(
            auto_now=True, blank=True, null=True, verbose_name="last updated"
        )
        objects = RestrictedQuerySet.as_manager()

        class Meta:
            abstract = True

        def snapshot(self):
            pass

    base.ChangeLoggedModel = ChangeLoggedModel
    return ChangeLoggedModel


base.__getattr__ = _getattr
sys.modules["netbox.models"] = base
users = types.ModuleType("users")
users_models = types.ModuleType("users.models")


def _users_getattr(name):
    if name not in {"User", "Group"}:
        raise AttributeError(name)
    from tests.support.users import models

    return getattr(models, name)


users_models.__getattr__ = _users_getattr
sys.modules["users"] = users
sys.modules["users.models"] = users_models
