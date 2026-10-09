from django.contrib.auth.models import AbstractUser, Permission
from django.db import models


class Group(models.Model):
    name = models.CharField(max_length=150, unique=True)
    permissions = models.ManyToManyField(Permission, related_name="test_groups", blank=True)


class User(AbstractUser):
    groups = models.ManyToManyField(Group, related_name="users", blank=True)
