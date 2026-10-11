from django.urls import include, path

from netbox_oa.maintenance_urls import urlpatterns as maintenance_patterns
from tests.support.netbox_api import install

install()

plugin_patterns = [path("oa/", include((maintenance_patterns, "netbox_oa"), namespace="netbox_oa"))]
urlpatterns = [path("plugins/", include((plugin_patterns, "plugins"), namespace="plugins"))]
urlpatterns += [path("api/", include("netbox_oa.api.urls"))]
