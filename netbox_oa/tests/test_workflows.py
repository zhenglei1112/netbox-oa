from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import ProtectedError
from django.test import TestCase
from django_fsm_log.models import StateLog
from users.models import Group

from netbox_oa import services
from netbox_oa.models import WorkflowDefinition, WorkflowInstance, WorkflowTask


class WorkflowTests(TestCase):
    def grant(self, user, model_name, action):
        from django.apps import apps
        from django.contrib.contenttypes.models import ContentType

        if apps.is_installed("users"):
            from users.models import ObjectPermission

            permission = ObjectPermission.objects.create(
                name=f"Test {action} {model_name}", actions=[action]
            )
            permission.object_types.add(
                ContentType.objects.get(app_label="netbox_oa", model=model_name)
            )
            permission.users.add(user)
        else:
            user.user_permissions.add(
                Permission.objects.get(
                    content_type__app_label="netbox_oa", codename=f"{action}_{model_name}"
                )
            )

    def setUp(self):
        self.requester = get_user_model().objects.create_superuser(
            username="requester", email="requester@example.test", password="test"
        )
        self.group = Group.objects.create(name="Approvers")
        self.second_group = Group.objects.create(name="Second approvers")
        self.definition = WorkflowDefinition.objects.create(
            name="Device change",
            slug="device-change",
            steps=[
                {"name": "First approval", "group_id": self.group.pk},
                {"name": "Second approval", "group_id": self.second_group.pk},
            ],
        )

    def create(self):
        return services.create_instance(
            actor=self.requester,
            definition=self.definition,
            title="Change request",
            payload={"device_id": 42},
        )

    def submit(self):
        instance = self.create()
        return services.submit_instance(pk=instance.pk, actor=self.requester)

    def test_sequential_approvals_and_audit(self):
        instance = self.submit()
        first = instance.tasks.get()
        self.assertEqual(instance.status, "pending")
        services.decide_task(pk=first.pk, actor=self.requester, decision="approve", comment="OK")
        instance = WorkflowInstance.objects.get(pk=instance.pk)
        self.assertEqual(instance.status, "pending")
        self.assertEqual(instance.current_step, 1)
        services.decide_task(
            pk=instance.tasks.get(status="pending").pk, actor=self.requester, decision="approve"
        )
        instance = WorkflowInstance.objects.get(pk=instance.pk)
        self.assertEqual(instance.status, "approved")
        log = StateLog.objects.for_(first).get()
        self.assertEqual(log.by, self.requester)
        self.assertEqual(log.description, "OK")
        self.assertEqual(StateLog.objects.for_(instance).count(), 2)

    def test_repeated_approval_rolls_back(self):
        instance = self.submit()
        first = instance.tasks.get()
        services.decide_task(pk=first.pk, actor=self.requester, decision="approve")
        with self.assertRaises(ValidationError):
            services.decide_task(pk=first.pk, actor=self.requester, decision="approve")
        self.assertEqual(instance.tasks.count(), 2)
        self.assertEqual(StateLog.objects.for_(first).count(), 1)

    def test_repeated_submission_rolls_back(self):
        instance = self.submit()
        with self.assertRaises(ValidationError):
            services.submit_instance(pk=instance.pk, actor=self.requester)
        self.assertEqual(instance.tasks.count(), 1)
        self.assertEqual(StateLog.objects.for_(instance).count(), 1)

    def test_rejection_ends_workflow(self):
        instance = self.submit()
        services.decide_task(
            pk=instance.tasks.get().pk, actor=self.requester, decision="reject", comment="No"
        )
        self.assertEqual(WorkflowInstance.objects.get(pk=instance.pk).status, "rejected")
        self.assertEqual(instance.tasks.count(), 1)

    def test_cancel_closes_pending_task(self):
        instance = self.submit()
        services.cancel_instance(pk=instance.pk, actor=self.requester, comment="Withdrawn")
        self.assertEqual(WorkflowInstance.objects.get(pk=instance.pk).status, "cancelled")
        self.assertEqual(instance.tasks.get().status, "cancelled")
        with self.assertRaises(ValidationError):
            services.decide_task(
                pk=instance.tasks.get().pk, actor=self.requester, decision="approve"
            )

    def test_definition_changes_do_not_change_running_workflow(self):
        instance = self.submit()
        self.definition.steps = [{"name": "Changed", "group_id": self.group.pk}]
        self.definition.save()
        services.decide_task(pk=instance.tasks.get().pk, actor=self.requester, decision="approve")
        self.assertEqual(
            instance.tasks.get(status="pending").assigned_group_id, self.second_group.pk
        )
        with self.assertRaises(ProtectedError):
            self.second_group.delete()

    def test_non_member_cannot_approve(self):
        instance = self.submit()
        actor = get_user_model().objects.create_user(username="outsider", password="test")
        self.grant(actor, "workflowtask", "change")
        with self.assertRaises(PermissionDenied):
            services.decide_task(pk=instance.tasks.get().pk, actor=actor, decision="approve")
        self.assertEqual(instance.tasks.get().status, "pending")

    def test_group_membership_alone_does_not_grant_permission(self):
        instance = self.submit()
        actor = get_user_model().objects.create_user(username="member", password="test")
        actor.groups.add(self.group)
        with self.assertRaises(PermissionDenied):
            services.decide_task(pk=instance.tasks.get().pk, actor=actor, decision="approve")

    def test_member_with_permission_can_approve(self):
        instance = self.submit()
        actor = get_user_model().objects.create_user(username="approver", password="test")
        actor.groups.add(self.group)
        self.grant(actor, "workflowtask", "change")
        task = services.decide_task(pk=instance.tasks.get().pk, actor=actor, decision="approve")
        self.assertEqual(task.decided_by, actor)

    def test_disabled_definition_cannot_start(self):
        self.definition.enabled = False
        self.definition.save()
        with self.assertRaises(ValidationError):
            self.create()
        self.assertEqual(WorkflowInstance.objects.count(), 0)

    def test_missing_future_group_rejects_submission(self):
        instance = self.create()
        self.second_group.delete()
        with self.assertRaises(ValidationError):
            services.submit_instance(pk=instance.pk, actor=self.requester)
        self.assertEqual(WorkflowInstance.objects.get(pk=instance.pk).status, "draft")
        self.assertEqual(WorkflowTask.objects.count(), 0)

    def test_invalid_payload_and_steps(self):
        with self.assertRaises(ValidationError):
            services.create_instance(
                actor=self.requester, definition=self.definition, title="Bad", payload=[]
            )
        for steps in ([], [{"name": "", "group_id": 1}], [{"name": "x", "group_id": True}]):
            self.definition.steps = steps
            with self.assertRaises(ValidationError):
                self.definition.full_clean()

    def test_non_owner_cannot_cancel(self):
        instance = self.submit()
        actor = get_user_model().objects.create_user(username="other", password="test")
        self.grant(actor, "workflowinstance", "change")
        with self.assertRaises(PermissionDenied):
            services.cancel_instance(pk=instance.pk, actor=actor)

    def test_object_constraint_denies_approval(self):
        instance = self.submit()
        task = instance.tasks.get()
        with patch.object(
            type(WorkflowTask.objects.all()), "restrict", return_value=WorkflowTask.objects.none()
        ):
            with self.assertRaises(PermissionDenied):
                services.decide_task(pk=task.pk, actor=self.requester, decision="approve")
        self.assertEqual(WorkflowTask.objects.get(pk=task.pk).status, "pending")

    def test_next_task_failure_rolls_back_decision_and_log(self):
        instance = self.submit()
        task = instance.tasks.get()
        with patch("netbox_oa.services._new_task", side_effect=ValidationError("failure")):
            with self.assertRaises(ValidationError):
                services.decide_task(pk=task.pk, actor=self.requester, decision="approve")
        self.assertEqual(WorkflowTask.objects.get(pk=task.pk).status, "pending")
        self.assertEqual(WorkflowInstance.objects.get(pk=instance.pk).current_step, 0)
        self.assertEqual(StateLog.objects.for_(task).count(), 0)

    def test_direct_state_assignment_is_protected(self):
        instance = self.create()
        with self.assertRaises(AttributeError):
            instance.status = "approved"
