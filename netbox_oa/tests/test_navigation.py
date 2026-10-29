from django.test import TestCase

from netbox_oa.navigation import menu_items


class NavigationTests(TestCase):
    def test_maintenance_order_menu_item_points_to_v2(self):
        item = next(m for m in menu_items if m.link_text == "维护服务工单")
        self.assertEqual(item.link, "plugins:netbox_oa:maintenance_workbench_v2")

