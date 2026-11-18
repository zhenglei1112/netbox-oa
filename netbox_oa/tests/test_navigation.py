from django.test import TestCase

from netbox_oa.navigation import menu, menu_items


class NavigationTests(TestCase):
    def test_top_level_menu_groups_service_orders_and_general_config(self):
        self.assertEqual(menu.label, "OA 工作流")
        self.assertEqual([label for label, _ in menu.groups], ["服务工单", "通用配置"])
        service_items = menu.groups[0][1]
        config_items = menu.groups[1][1]
        self.assertEqual(
            [item.link_text for item in config_items],
            ["流程定义", "流程实例", "审批任务"],
        )
        self.assertEqual(
            [item.link_text for item in service_items],
            ["工单台账", "维护服务工单", "发起维护工单", "维护流程配置"],
        )
        self.assertEqual(service_items + config_items, menu_items)

    def test_maintenance_order_menu_item_points_to_v2(self):
        item = next(m for m in menu_items if m.link_text == "维护服务工单")
        self.assertEqual(item.link, "plugins:netbox_oa:maintenance_workbench_v2")

    def test_ledger_menu_requires_ledger_view_permission(self):
        item = next(m for m in menu_items if m.link_text == "工单台账")
        self.assertEqual(item.link, "plugins:netbox_oa:workorderledger_list")
        self.assertEqual(item.permissions, ["netbox_oa.view_workorderledger"])

