"""Minimal tags for rendering the captured native NetBox form templates in domain tests."""

from django import template

register = template.Library()


@register.filter()
def meta(model, attr):
    return getattr(model._meta, attr, "")


@register.filter()
def startswith(value, prefix):
    return value.startswith(prefix)


@register.filter()
def getfield(form, name):
    return form[name] if name in form.fields else None


@register.filter()
def widget_type(field):
    return field.field.widget.__class__.__name__.lower()


@register.inclusion_tag("form_helpers/render_field.html")
def render_field(field):
    return {"field": field, "label": field.label}


@register.inclusion_tag("form_helpers/render_form.html")
def render_form(form):
    return {"form": form}


@register.inclusion_tag("form_helpers/render_errors.html")
def render_errors(form):
    return {"form": form}


@register.simple_tag
def render_fieldset(form, fieldset):
    raise NotImplementedError("The standalone form harness does not use fieldsets.")


@register.simple_tag
def render_custom_fields(form):
    raise NotImplementedError("The standalone form harness does not use custom field forms.")
