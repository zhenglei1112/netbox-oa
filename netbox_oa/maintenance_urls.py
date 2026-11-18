from django.urls import path
from netbox.views import generic

from . import contract_views
from . import maintenance_billing_views as billing_views
from . import maintenance_signature_views as signatures
from . import maintenance_views as views
from .ledger import WorkOrderLedgerView
from .models import MaintenanceOrder

urlpatterns = [
    path("maintenance/ledger/", WorkOrderLedgerView.as_view(), name="workorderledger_list"),
    path("maintenance/ledger/", WorkOrderLedgerView.as_view(), name="maintenanceorder_list"),
    path("maintenance/preferences/guide/",
         views.MaintenanceGuidePreferenceView.as_view(), name="maintenance_guide_preference"),
    path("maintenance/orders/<int:pk>/settlement-billing/new/",
         billing_views.SettlementBillingCreateView.as_view(), name="settlement_billing_create"),
    path("maintenance/orders/<int:pk>/settlement-billing/",
         billing_views.SettlementBillingListView.as_view(), name="settlement_billing_list"),
    path("maintenance/materials/<int:pk>/billing/",
         billing_views.SettlementBillingEditView.as_view(), name="settlement_billing_edit"),
    path(
        "maintenance/orders/<int:pk>/changelog/",
        generic.ObjectChangeLogView.as_view(
            base_template="netbox_oa/maintenance/changelog.html"
        ),
        kwargs={"model": MaintenanceOrder},
        name="maintenanceorder_changelog",
    ),
    path(
        "maintenance/tasks/<int:pk>/quick-complete/",
        views.MaintenanceTaskQuickCompleteView.as_view(),
        name="maintenancetask_quick_complete",
    ),
    path(
        "contracts/<int:contract_id>/orders/<int:pk>/payments/",
        contract_views.ContractOrderPaymentsView.as_view(),
        name="contract_order_payments",
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
        views.MaintenanceWorkbenchView.as_view(),
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
