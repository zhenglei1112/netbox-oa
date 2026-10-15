from netbox.plugins import PluginConfig

__version__ = "0.2.1"


class NetBoxOAConfig(PluginConfig):
    name = "netbox_oa"
    verbose_name = "OA 工作流"
    description = "轻量流程管理与顺序审批"
    version = __version__
    base_url = "oa"
    min_version = "4.4.2"
    max_version = "4.6.99"
    django_apps = ["django_fsm_log"]
    default_auto_field = "django.db.models.BigAutoField"
    default_settings = {"contract_service_region_field": "ProvincesOfResources"}


config = NetBoxOAConfig
