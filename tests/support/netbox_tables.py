"""Minimal NetBox base columns; sorting/pagination/rendering use real django-tables2."""

import django_tables2 as tables


class NetBoxTable(tables.Table):
    pk = tables.Column(visible=False)
    id = tables.Column(linkify=True)
    actions = tables.Column(empty_values=(), orderable=False)

    class Meta:
        attrs = {"class": "table table-hover object-list"}
