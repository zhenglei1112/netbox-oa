"""Minimal external-contract schema for OA integration tests."""

from django.db import models
from django.urls import reverse
from netbox.models import ChangeLoggedModel


class ServiceProvider(ChangeLoggedModel):
    name = models.CharField(max_length=100)

    def __str__(self):
        return self.name


class ContractType(ChangeLoggedModel):
    name = models.CharField(max_length=100)


class Contract(ChangeLoggedModel):
    contract_type = models.ForeignKey(ContractType, null=True, blank=True, on_delete=models.PROTECT)
    name = models.CharField(max_length=100)
    number = models.CharField(max_length=100, blank=True)
    external_party_object = models.ForeignKey(ServiceProvider, on_delete=models.PROTECT)
    custom_field_data = models.JSONField(default=dict)
    maintenance_pricing = models.JSONField(default=dict, blank=True)


class Invoice(ChangeLoggedModel):
    number = models.CharField(max_length=100)
    template = models.BooleanField(null=True, blank=True, default=False)
    status = models.CharField(
        max_length=50,
        default="posted",
        choices=(("draft", "Draft"), ("posted", "Posted"), ("canceled", "Canceled")),
    )
    date = models.DateField(null=True, blank=True)
    contracts = models.ManyToManyField(Contract, related_name="invoices", blank=True)
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True)
    currency = models.CharField(max_length=3, default="CNY")
    amount = models.DecimalField(max_digits=10, decimal_places=2)

    def get_absolute_url(self):
        return reverse("plugins:netbox_contract:invoice", args=[self.pk])
