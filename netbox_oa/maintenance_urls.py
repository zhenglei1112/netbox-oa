from django.urls import path

from . import maintenance_views as views

urlpatterns = [
    path(
        "maintenance/orders/<int:pk>/forms/<str:material>/",
        views.MaintenanceCompanyFormView.as_view(),
        name="maintenance_company_form",
    ),
    path(
        "maintenance/materials/<int:pk>/print/",
        views.MaintenancePrintView.as_view(),
        name="maintenance_print",
    ),
    path(
        "maintenance/definition/",
        views.MaintenanceFlowDefinitionView.as_view(),
        name="maintenance_flow_definition",
    ),
    path(
        "maintenance/orders/<int:pk>/edit/",
        views.MaintenanceOrderEditView.as_view(),
        name="maintenanceorder_edit",
    ),
    path("maintenance/", views.MaintenanceWorkbenchView.as_view(), name="maintenance_workbench"),
    path(
        "maintenance/orders/add/",
        views.MaintenanceOrderCreateView.as_view(),
        name="maintenanceorder_add",
    ),
    path(
        "maintenance/orders/<int:pk>/",
        views.MaintenanceWorkbenchView.as_view(),
        name="maintenanceorder",
    ),
    path(
        "maintenance/orders/<int:pk>/<str:operation>/",
        views.MaintenanceOrderActionView.as_view(),
        name="maintenanceorder_action",
    ),
    path(
        "maintenance/tasks/<int:pk>/<str:operation>/",
        views.MaintenanceTaskActionView.as_view(),
        name="maintenancetask_action",
    ),
    path(
        "maintenance/materials/<int:pk>/download/",
        views.MaintenanceDownloadView.as_view(),
        name="maintenance_download",
    ),
    path(
        "maintenance/upload/<int:pk>/",
        views.MaintenanceUploadView.as_view(),
        name="maintenance_upload",
    ),
    path(
        "maintenance/link/<int:pk>/", views.MaintenanceLinkView.as_view(), name="maintenance_link"
    ),
    path(
        "maintenance/policies/", views.MaintenancePolicyView.as_view(), name="maintenancepolicy_add"
    ),
    path(
        "maintenance/policies/<int:pk>/",
        views.MaintenancePolicyView.as_view(),
        name="maintenancepolicy",
    ),
]
