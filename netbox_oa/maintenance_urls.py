from django.urls import path

from . import contract_views
from . import maintenance_signature_views as signatures
from . import maintenance_v3 as v3
from . import maintenance_views as views

urlpatterns = [
    path("maintenance/v3/", v3.MaintenanceV3ListView.as_view(), name="maintenance_v3_list"),
    path(
        "maintenance/v3/orders/add/", v3.MaintenanceV3EditView.as_view(),
        name="maintenance_v3_add",
    ),
    path(
        "maintenance/v3/orders/<int:pk>/", v3.MaintenanceV3DetailView.as_view(),
        name="maintenance_v3_detail",
    ),
    path(
        "maintenance/v3/orders/<int:pk>/edit/", v3.MaintenanceV3EditView.as_view(),
        name="maintenance_v3_edit",
    ),
    path(
        "maintenance/v3/orders/<int:pk>/forms/<str:material>/",
        v3.MaintenanceV3CompanyFormView.as_view(), name="maintenance_v3_company_form",
    ),
    path(
        "maintenance/v3/orders/<int:pk>/signature/",
        v3.MaintenanceV3DraftSignatureView.as_view(), name="maintenance_v3_draft_signature",
    ),
    path(
        "contracts/<int:contract_id>/orders/<int:pk>/flow/",
        contract_views.ContractOrderFlowView.as_view(),
        name="contract_order_flow",
    ),
    path(
        "contracts/<int:contract_id>/orders/<int:pk>/materials/",
        contract_views.ContractOrderMaterialsView.as_view(),
        name="contract_order_materials",
    ),
    path(
        "maintenance/v2/",
        views.MaintenanceWorkbenchV2View.as_view(),
        name="maintenance_workbench_v2",
    ),
    path(
        "maintenance/materials/<int:pk>/print-event/",
        views.MaintenancePrintEventView.as_view(),
        name="maintenance_print_event",
    ),
    path(
        "maintenance/materials/<int:pk>/remove/",
        views.MaintenanceMaterialRemoveView.as_view(),
        name="maintenance_material_remove",
    ),
    path(
        "maintenance/orders/<int:pk>/materials/<str:material>/upload/",
        views.MaintenanceMaterialUploadView.as_view(),
        name="maintenance_material_upload",
    ),
    path(
        "maintenance/materials/<int:pk>/verify/",
        views.MaintenanceVerificationView.as_view(),
        name="maintenance_verify",
    ),
    path(
        "maintenance/orders/<int:pk>/review/",
        views.MaintenanceReviewView.as_view(),
        name="maintenance_review",
    ),
    path(
        "maintenance/materials/<int:pk>/preview/",
        views.MaintenancePreviewView.as_view(),
        name="maintenance_preview",
    ),
    path(
        "maintenance/tasks/<int:pk>/signature/",
        signatures.SignatureCreateView.as_view(),
        name="maintenance_signature_create",
    ),
    path(
        "maintenance/signatures/<int:pk>/",
        signatures.SignatureView.as_view(),
        name="maintenance_signature",
    ),
    path(
        "maintenance/signatures/<int:pk>/status/",
        signatures.SignatureStatusView.as_view(),
        name="maintenance_signature_status",
    ),
    path(
        "maintenance/signatures/<int:pk>/image/",
        signatures.SignatureImageView.as_view(),
        name="maintenance_signature_image",
    ),
    path(
        "maintenance/sign/<str:token>/",
        signatures.MobileSignatureView.as_view(),
        name="maintenance_signature_mobile",
    ),
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
