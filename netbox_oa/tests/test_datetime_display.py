"""Display formats stay stable across locales without altering stored dates."""

from datetime import date, datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from django.template.loader import render_to_string
from django.test import SimpleTestCase
from django.utils import timezone as django_timezone
from django.utils import translation

from netbox_oa.templatetags.oa_filters import activity_time, oa_datetime


class DateTimeDisplayTests(SimpleTestCase):
    def test_datetimes_use_current_timezone_and_seconds(self):
        value = datetime(2026, 4, 10, 8, 21, 32, 123456, tzinfo=timezone.utc)
        with django_timezone.override(ZoneInfo("Asia/Shanghai")):
            self.assertEqual(oa_datetime(value), "2026-04-10 16:21:32")
            self.assertEqual(oa_datetime(value.isoformat()), "2026-04-10 16:21:32")
            self.assertEqual(activity_time(value), "2026-04-10 16:21:32")

    def test_naive_snapshot_preserves_wall_time_and_date_only_has_no_invented_time(self):
        self.assertEqual(oa_datetime("2026-04-10T16:21:32"), "2026-04-10 16:21:32")
        self.assertEqual(oa_datetime("2026-04-10 16:21"), "2026-04-10 16:21:00")
        self.assertEqual(oa_datetime("2026-04-10"), "2026-04-10")
        self.assertEqual(oa_datetime(date(2026, 4, 10)), "2026-04-10")
        for value in ("", None, "____-__-__", "not-a-date", "2026-13-40"):
            self.assertEqual(oa_datetime(value), value)

    def test_object_header_does_not_inherit_netbox_locale_or_iso_format(self):
        value = datetime(2026, 4, 10, 8, 21, 32, tzinfo=timezone.utc)
        for locale in ("en", "zh-hans"):
            with translation.override(locale), django_timezone.override(ZoneInfo("Asia/Shanghai")):
                html = render_to_string("netbox_oa/inc/object_timestamps.html", {
                    "object": SimpleNamespace(created=value, last_updated=value),
                })
                self.assertEqual(html.count("2026-04-10 16:21:32"), 2)
                self.assertNotIn("2026-04-10T", html)
                self.assertNotIn("+08:00", html)
