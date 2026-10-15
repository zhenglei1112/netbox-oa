from django.db import models
from netbox.models import ChangeLoggedModel


class Region(ChangeLoggedModel):
    name = models.CharField(max_length=100)

    def __str__(self):
        return self.name
