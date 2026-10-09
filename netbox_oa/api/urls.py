from netbox.api.routers import NetBoxRouter

from . import views

app_name = "netbox_oa"
router = NetBoxRouter()
router.register("definitions", views.WorkflowDefinitionViewSet)
router.register("instances", views.WorkflowInstanceViewSet)
router.register("tasks", views.WorkflowTaskViewSet)
urlpatterns = router.urls
