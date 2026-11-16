from django.template.loader import render_to_string
from django.test import SimpleTestCase

from netbox_oa.templatetags.oa_filters import billing_service_period


class BillingServicePeriodTests(SimpleTestCase):
    def test_uses_earliest_start_and_latest_end_across_documents(self):
        snapshot = {"documents": [
            {"billing_data": {"lines": [
                {"started_at": "2026-09-06", "ended_at": "2026-09-10"},
                {"started_at": "2026-09-04T08:00", "ended_at": ""},
            ]}},
            {"billing_data": {"lines": [
                {"started_at": None, "ended_at": "2026-09-08T18:00"},
            ]}},
        ]}
        expected = "2026年9月4日至2026年9月10日"
        self.assertEqual(billing_service_period(snapshot), expected)
        html = render_to_string("netbox_oa/maintenance/acceptance_print_table.html", {
            "data": {"settlement_billing": snapshot},
        })
        self.assertIn(expected, html)
        self.assertEqual(html.count(">服务时间</th>"), 1)
        self.assertLess(html.index('class="service-items-row"'),
                        html.index('class="billing-service-period d-print-none"'))
        self.assertLess(html.index(expected), html.index("验收结论"))
        self.assertNotIn("billing-time-column", html)

    def test_missing_equal_and_legacy_dates(self):
        for lines, expected in (
            ([], ""),
            ([{}], ""),
            ([{"started_at": None, "ended_at": ""}], ""),
            ([{"started_at": "2026-09-04"}], "2026年9月4日"),
            ([{"ended_at": "2026-09-10"}], "2026年9月10日"),
            ([{"started_at": "2026-09-04T08:00", "ended_at": "2026-09-04T18:00"}],
             "2026年9月4日"),
            ([{"started_at": "invalid", "ended_at": "2026-02-30"}], ""),
        ):
            with self.subTest(lines=lines):
                self.assertEqual(billing_service_period({"lines": lines}), expected)
        self.assertEqual(billing_service_period(None), "")

    def test_settlement_table_has_summary_and_seven_detail_columns(self):
        html = render_to_string("netbox_oa/maintenance/settlement_billing_table.html", {
            "billing": {"lines": [
                {"started_at": "2026-09-04", "ended_at": "2026-09-10"},
            ]},
        })
        self.assertIn("2026年9月4日至2026年9月10日", html)
        self.assertEqual(html.count(">服务时间</th>"), 1)
        self.assertIn('<th colspan="6">合计（元）</th>', html)
        self.assertEqual(html.split("<tbody>")[1].split("</tbody>")[0].count("<td"), 7)
