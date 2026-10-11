import os

from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.utils.deconstruct import deconstructible


@deconstructible
class MaintenanceDocumentStorage(FileSystemStorage):
    """Outside MEDIA_ROOT; downloads go through an authenticated permission check."""

    def __init__(self):
        config = getattr(settings, "PLUGINS_CONFIG", {}).get("netbox_oa", {})
        location = config.get("maintenance_storage_root") or os.path.join(
            os.path.dirname(settings.MEDIA_ROOT), "netbox_oa_private"
        )
        super().__init__(location=location)

    def url(self, name):
        raise ValueError("Private documents must use the authorized download endpoint.")
