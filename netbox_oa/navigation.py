from netbox.plugins import PluginMenu, PluginMenuItem

service_order_items = (
    PluginMenuItem(
        link="plugins:netbox_oa:maintenance_flow_definition",
        link_text="服务工单流程介绍",
        permissions=["netbox_oa.view_maintenanceorder"],
    ),
    PluginMenuItem(
        link="plugins:netbox_oa:maintenance_workbench_v2",
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
)

general_config_items = (
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

menu_items = service_order_items + general_config_items

menu = PluginMenu(
    label="OA 工作流",
    groups=(
        ("服务工单", service_order_items),
        ("通用配置", general_config_items),
    ),
    icon_class="mdi mdi-clipboard-flow-outline",
)
