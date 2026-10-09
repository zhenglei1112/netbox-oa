from netbox.plugins import PluginMenuItem

menu_items = (
    PluginMenuItem(
        link="plugins:netbox_oa:workflowdefinition_list",
        link_text="流程定义",
        permissions=["netbox_oa.view_workflowdefinition"],
    ),
    PluginMenuItem(
        link="plugins:netbox_oa:workflowinstance_list",
        link_text="流程实例",
        permissions=["netbox_oa.view_workflowinstance"],
    ),
    PluginMenuItem(
        link="plugins:netbox_oa:workflowtask_list",
        link_text="审批任务",
        permissions=["netbox_oa.view_workflowtask"],
    ),
)
