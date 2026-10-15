from django.http import HttpResponse
from django.urls import include, path

from netbox_oa.maintenance_urls import urlpatterns as maintenance_patterns
from tests.support.netbox_api import install

install()

plugin_patterns = [path("oa/", include((maintenance_patterns, "netbox_oa"), namespace="netbox_oa"))]
# URL-only stand-ins; native contract forms remain owned by netbox-contract.
contract_patterns = [
    path("contracts/<int:pk>/", lambda request, pk: HttpResponse(status=204), name="contract"),
    path("invoices/add/", lambda request: HttpResponse(status=204), name="invoice_add"),
    path("invoices/<int:pk>/", lambda request, pk: HttpResponse(status=204), name="invoice"),
    path(
        "invoices/<int:pk>/edit/", lambda request, pk: HttpResponse(status=204), name="invoice_edit"
    ),
]
plugin_patterns.append(
    path("contracts/", include((contract_patterns, "netbox_contract"), namespace="netbox_contract"))
)
urlpatterns = [path("plugins/", include((plugin_patterns, "plugins"), namespace="plugins"))]
urlpatterns += [path("api/", include("netbox_oa.api.urls"))]
