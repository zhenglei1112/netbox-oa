from decimal import Decimal

from django.template import Context, Template
from django.test import SimpleTestCase

from netbox_oa.templatetags.oa_filters import duration_hours
from netbox_oa.time_display import format_duration


class DurationDisplayTests(SimpleTestCase):
    def test_minute_hour_and_day_boundaries(self):
        for minutes, expected in (
            (-1, "0分钟"), (0, "0分钟"), (59, "59分钟"),
            (60, "1小时0分钟"), (1439, "23小时59分钟"),
            (1440, "1天0小时0分钟"), (1441, "1天0小时1分钟"),
            (158 * 60 + 24, "6天14小时24分钟"),
        ):
            with self.subTest(minutes=minutes):
                self.assertEqual(format_duration(minutes), expected)

    def test_decimal_hour_values_use_the_same_rules(self):
        for hours, expected in (
            (Decimal("9.5"), "9小时30分钟"), ("158.4", "6天14小时24分钟"),
            (24, "1天0小时0分钟"), (0, "0分钟"),
            (Decimal(1) / 3600, "0分钟"),
        ):
            with self.subTest(hours=hours):
                self.assertEqual(duration_hours(hours), expected)

    def test_missing_and_invalid_values_are_preserved(self):
        for value in (None, "", "invalid", "NaN", "Infinity"):
            with self.subTest(value=value):
                self.assertEqual(duration_hours(value), value)

    def test_hour_filter_is_available_in_templates(self):
        rendered = Template("{% load oa_filters %}{{ hours|duration_hours }}").render(
            Context({"hours": "48.5"})
        )
        self.assertEqual(rendered, "2天0小时30分钟")
