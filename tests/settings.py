"""Standalone domain test harness, not a substitute for NetBox integration tests.

Only the NetBox plugin config, base model and users module are replaced. Django,
FSM, FSM logging, database migrations and workflow code remain real.
"""

import sys
import types
from pathlib import Path

from django import forms
from django.apps import AppConfig

SECRET_KEY = "local-tests-only"
INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "tests.support.users.apps.UsersConfig",
    "tests.support.contracts.apps.ContractsConfig",
    "tests.support.dcim.apps.DCIMConfig",
    "django_fsm_log",
    "netbox_oa",
]
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
AUTH_USER_MODEL = "users.User"
AUTHENTICATION_BACKENDS = ["tests.support.backends.TestPermissionBackend"]
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
MEDIA_ROOT = str(Path(__file__).resolve().parent.parent / ".tmp" / "media")
TIME_ZONE = "Asia/Shanghai"
ROOT_URLCONF = "tests.urls"
ALLOWED_HOSTS = ["testserver", "localhost"]
STATIC_URL = "/static/"
LOGIN_URL = "/login/"
MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "DIRS": [str(Path(__file__).resolve().parent / "support" / "templates")],
        "OPTIONS": {
            "libraries": {
                "form_helpers": "tests.support.form_helpers",
                "helpers": "tests.support.form_helpers",
            },
            "builtins": ["tests.support.form_helpers"],
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]

netbox = types.ModuleType("netbox")
plugins = types.ModuleType("netbox.plugins")
plugins.PluginConfig = AppConfig
sys.modules["netbox"] = netbox
sys.modules["netbox.plugins"] = plugins

class ChangelogMessageMixin(forms.Form):
    changelog_message = forms.CharField(required=False, max_length=200)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.meta_fields = ["changelog_message"]


netbox_forms = types.ModuleType("netbox.forms")
netbox_form_mixins = types.ModuleType("netbox.forms.mixins")
netbox_form_mixins.ChangelogMessageMixin = ChangelogMessageMixin
sys.modules["netbox.forms"] = netbox_forms
sys.modules["netbox.forms.mixins"] = netbox_form_mixins

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

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["rest_framework.authentication.SessionAuthentication"]
}
