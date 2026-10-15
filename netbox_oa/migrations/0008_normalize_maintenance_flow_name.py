from django.db import migrations


def normalize_flow_name(apps, schema_editor):
    alias = schema_editor.connection.alias
    descriptions = ("发起四阶段维护流程", "发起维护流程")
    replacement = "发起代维单位维护服务工单流程"
    events = apps.get_model("netbox_oa", "MaintenanceEvent")
    events.objects.using(alias).filter(action="start", description__in=descriptions).update(
        description=replacement
    )
    logs = apps.get_model("django_fsm_log", "StateLog")
    logs.objects.using(alias).filter(
        content_type__app_label="netbox_oa",
        content_type__model="maintenanceorder",
        transition="start",
        description__in=descriptions,
    ).update(description=replacement)


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_oa", "0007_maintenancepolicy_is_default_and_more"),
        ("django_fsm_log", "0005_description_null"),
    ]
    operations = [migrations.RunPython(normalize_flow_name, migrations.RunPython.noop)]
