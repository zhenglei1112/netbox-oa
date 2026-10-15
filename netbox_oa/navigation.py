from netbox.plugins import PluginMenuItem

from .maintenance_flow import FLOW_NAME

menu_items = (
    PluginMenuItem(
        link="plugins:netbox_oa:maintenance_flow_definition",
        link_text=FLOW_NAME,
        permissions=["netbox_oa.view_maintenanceorder"],
    ),
    PluginMenuItem(
        link="plugins:netbox_oa:maintenance_workbench",
        link_text="维护服务工单",
        permissions=["netbox_oa.view_maintenanceorder"],
    ),
    PluginMenuItem(
        link="plugins:netbox_oa:maintenanceorder_add",
        link_text="发起维护工单",
        permissions=["netbox_oa.add_maintenanceorder"],
    ),
    PluginMenuItem(
        link="plugins:netbox_oa:maintenancepolicy_add",
        link_text="维护流程配置",
        permissions=["netbox_oa.add_maintenancepolicy"],
    ),
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
