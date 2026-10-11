from netbox.api.routers import NetBoxRouter

from . import views
from .maintenance import DocumentViewSet, OrderViewSet, PolicyViewSet, TaskViewSet

app_name = "netbox_oa"
router = NetBoxRouter()
router.register("definitions", views.WorkflowDefinitionViewSet)
router.register("instances", views.WorkflowInstanceViewSet)
router.register("tasks", views.WorkflowTaskViewSet)
router.register("maintenance/policies", PolicyViewSet)
router.register("maintenance/orders", OrderViewSet)
router.register("maintenance/tasks", TaskViewSet)
router.register("maintenance/documents", DocumentViewSet)
urlpatterns = router.urls
