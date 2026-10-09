from django.contrib.auth.backends import ModelBackend
from django.contrib.auth.models import Permission


class TestPermissionBackend(ModelBackend):
    def _get_group_permissions(self, user_obj):
        return Permission.objects.filter(test_groups__users=user_obj)
