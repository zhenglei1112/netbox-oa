"""Native template tag adapters for isolated browser rendering without NetBox's registry."""

from django import template
from django.urls import reverse

from tests.support.form_helpers import meta

register = template.Library()
register.filter("meta", meta)
register.filter("bettertitle", lambda value: str(value).title())
register.filter("isodatetime", lambda value, arg=None: str(value or ""))
register.filter("get_key", lambda value, key: value.get(key) if hasattr(value, "get") else None)


@register.simple_tag
def action_url(obj, action):
    return reverse("plugins:netbox_oa:maintenance_v3_list")


def empty(*args, **kwargs):
    return ""


for name in (
    "plugin_buttons", "plugin_list_buttons", "plugin_alerts", "custom_links", "model_view_tabs",
    "bookmark_button", "subscribe_button", "action_buttons", "applied_filters", "table_config_form",
    "badge",
):
    register.simple_tag(empty, name=name)


@register.simple_tag(takes_context=True)
def querystring(context, request=None, **kwargs):
    query = (request or context["request"]).GET.copy()
    for key, value in kwargs.items():
        query[key] = value
    return "?" + query.urlencode()
