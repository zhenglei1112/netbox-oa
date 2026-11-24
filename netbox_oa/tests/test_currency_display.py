from decimal import Decimal

from django.template.loader import render_to_string
from django.test import SimpleTestCase

from netbox_oa.maintenance_billing import UNITS
from netbox_oa.templatetags.oa_filters import billing_unit, cny_number


class CurrencyDisplayTests(SimpleTestCase):
    def test_currency_number_and_rounding(self):
        self.assertEqual(cny_number(Decimal("2200")), "￥2,200.00")
        self.assertEqual(cny_number("1234.565"), "￥1,234.57")
        self.assertEqual(cny_number("-0.001"), "￥0.00")

    def test_empty_or_invalid_values_do_not_become_zero(self):
        for value in ("", None, "invalid", "NaN", "Infinity", "9999999999999999.999"):
            with self.subTest(value=value):
                self.assertEqual(cny_number(value), value)

    def test_acceptance_amount_and_billing_units(self):
        html = render_to_string("netbox_oa/maintenance/acceptance_print_table.html", {
            "data": {
                "settlement_amount": "2200.00",
                "settlement_billing": {"documents": [{"billing_data": {"lines": []}}],
                                       "total": "2200.00"},
            },
        })
        self.assertIn("￥2,200.00", html)
        self.assertNotIn("贰仟贰佰元整", html)
        self.assertIn('结算明细<span class="billing-unit-label">（单位：元）</span>', html)
        self.assertNotIn("（元）", html)
        for label in ("序号", "计费单价", "小计", "扣费金额", "结算价格"):
            self.assertIn(f">{label}</th>", html)

    def test_km_unit_displays_as_km_for_new_and_legacy_snapshots(self):
        self.assertEqual(UNITS["km"], "km")
        self.assertEqual(billing_unit("公里"), "km")
        self.assertEqual(billing_unit("km"), "km")
        self.assertEqual(billing_unit("次"), "次")
        line = {"name": "光缆", "unit_price": "5000", "quantity": "0.795",
                "unit": "公里", "gross": "3975.00", "deduction": "0.00", "net": "3975.00"}
        print_html = render_to_string("netbox_oa/maintenance/acceptance_print_table.html", {
            "data": {"settlement_billing": {"documents": [{"billing_data": {"lines": [line]}}],
                                            "total": "3975.00"}},
        })
        table_html = render_to_string("netbox_oa/maintenance/settlement_billing_table.html", {
            "billing": {"lines": [line], "total": "3975.00"},
        })
        for html in (print_html, table_html):
            self.assertIn("0.795 km", html)
            self.assertNotIn("公里", html)
