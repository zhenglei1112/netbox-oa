from django.urls import path

from . import views

app_name = "netbox_oa"
urlpatterns = [
    path(
        "definitions/", views.WorkflowDefinitionListView.as_view(), name="workflowdefinition_list"
    ),
    path(
        "definitions/add/",
        views.WorkflowDefinitionEditView.as_view(),
        name="workflowdefinition_add",
    ),
    path(
        "definitions/<int:pk>/", views.WorkflowDefinitionView.as_view(), name="workflowdefinition"
    ),
    path(
        "definitions/<int:pk>/edit/",
        views.WorkflowDefinitionEditView.as_view(),
        name="workflowdefinition_edit",
    ),
    path("instances/", views.WorkflowInstanceListView.as_view(), name="workflowinstance_list"),
    path("instances/add/", views.WorkflowInstanceCreateView.as_view(), name="workflowinstance_add"),
    path("instances/<int:pk>/", views.WorkflowInstanceView.as_view(), name="workflowinstance"),
    path("tasks/", views.WorkflowTaskListView.as_view(), name="workflowtask_list"),
    path("tasks/<int:pk>/", views.WorkflowTaskView.as_view(), name="workflowtask"),
    path("<str:kind>/<int:pk>/<str:operation>/", views.WorkflowActionView.as_view(), name="action"),
]
