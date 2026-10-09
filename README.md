# NetBox OA

面向 **NetBox 4.4.2** 的轻量工作流插件，Python 包为 `netbox_oa`。
当前兼容范围限定为 NetBox 4.4.x；其他版本需要单独集成验证。

提供流程定义、草稿申请、顺序审批、拒绝、撤销、审批组校验和 FSM 转换历史。
每个节点由指定 NetBox 用户组中的任意一名有权限的成员处理。
当前框架不包含会签、条件分支、退回重提、通知、定时任务或对 NetBox 业务对象的自动写回。

## 安装

使用 NetBox 的 Python 环境安装本项目，不要通过 PyPI 安装同名的 NetBox 包：

```bash
/opt/netbox/venv/bin/pip install /path/to/netbox-oa
```

在 NetBox `configuration.py` 中启用插件，保留原有插件：

```python
PLUGINS = ["netbox_oa"]
```

插件通过 `PluginConfig.django_apps` 自动注册 `django_fsm_log`，无需手工改写
NetBox 的 `INSTALLED_APPS`。在 `local_requirements.txt` 中加入本项目路径，
确保 NetBox 升级重建虚拟环境时仍会安装插件。

```bash
cd /opt/netbox/netbox
../venv/bin/python manage.py migrate
../venv/bin/python manage.py collectstatic --no-input
../venv/bin/python manage.py check
```

按部署方式重启 NetBox Web 进程与 worker。原生页面入口为 `/plugins/oa/` 下的
`definitions/`、`instances/`、`tasks/`，API 前缀为 `/api/plugins/oa/`。

## 最小使用流程

1. 在 NetBox 中创建审批用户组，将审批人加入对应组并授予对象权限。
2. 在 OA 工作流菜单中创建流程定义。`steps` 为按顺序执行的 JSON 数组：

   ```json
   [
     {"name": "部门审批", "group_id": 1},
     {"name": "运维审批", "group_id": 2}
   ]
   ```

3. 创建流程实例，填写标题、定义和业务 JSON 数据，保存后进入草稿详情。
4. 申请人提交草稿，系统仅生成当前节点的待办任务。
5. 审批组成员通过任务详情执行通过或拒绝；通过后生成下一节点的任务。
6. 最后一个节点通过后流程完成；申请人或超级管理员可撤销草稿和审批中流程。

## 权限与一致性

使用 NetBox 的对象权限，既校验模型权限，也通过 `restrict()` 校验对象约束。
建议申请人的实例 `view/change` 权限约束为 `{"requester_id": "$user"}`，
审批人的任务 `view/change` 权限约束为对应 `assigned_group_id`。

| 角色 | 对象类型与操作 |
| --- | --- |
| 流程管理员 | WorkflowDefinition 的 view/add/change/delete |
| 申请人 | WorkflowDefinition 的 view；WorkflowInstance 的 view/add/change |
| 审批人 | WorkflowTask 的 view/change；按需授予关联实例的 view |

审批人还必须属于当前任务的审批组；超级管理员可代审批。
流程实例不提供普通 API 编辑或删除，任务仅提供查询和审批动作。
状态、申请人、处理人、快照均由服务层维护，不接受客户端直接修改。
API 动作保留 NetBox 令牌的写权限限制，页面动作仅支持 POST 并使用 CSRF 保护。

提交时冻结节点快照，定义的后续修改不影响在途流程。事务内先锁实例、再锁任务；
重复或非法审批会回滚。数据库约束保证同一流程最多一个待审批任务、同一节点只能生成一次。
审批组通过保留表及 PROTECT 外键保护，尚未到达的节点也不会因组被删除而失效。
业务数据目前保存为 JSON，后续可在 `services.py` 扩展具体业务集成。

## REST API

| 路径（相对于 API 前缀） | 方法 | 用途 |
| --- | --- | --- |
| `definitions/`、`definitions/{id}/` | 标准 CRUD | 流程定义 |
| `instances/` | GET / POST | 查询 / 创建草稿 |
| `instances/{id}/` | GET | 实例详情 |
| `instances/{id}/submit/` | POST | 提交审批 |
| `instances/{id}/cancel/` | POST | 撤销 |
| `tasks/`、`tasks/{id}/` | GET | 查询任务 |
| `tasks/{id}/approve/`、`tasks/{id}/reject/` | POST | 审批 |

创建草稿的请求示例：

```json
{"title": "设备变更申请", "definition": 1, "payload": {"device_id": 42}}
```

动作请求支持 `{"comment": "同意"}`，意见最大 4000 字符。
实例支持 `status`、`definition_id`、`requester_id` 过滤；任务支持
`status`、`instance_id`、`assigned_group_id` 过滤。
审计历史在实例和任务详情中显示，由 `django-fsm-log` 保存状态、操作者、时间及意见。

## 开发与验证

```powershell
uv venv .venv --python 3.12
uv pip install --python .venv/Scripts/python.exe -e '.[dev]'
.venv/Scripts/python.exe -m ruff check .
.venv/Scripts/python.exe -m django test netbox_oa.tests --settings=tests.settings
.venv/Scripts/python.exe -m django makemigrations netbox_oa --check --dry-run --settings=tests.settings
.venv/Scripts/python.exe -m build --no-isolation
```

独立测试使用 SQLite 和精简的 NetBox 基类/用户替身，实际执行 Django ORM、迁移、
FSM 和 FSM 日志，验证审批行为与回滚。它不验证真实 NetBox 的对象权限后端、
HTTP API、模板集成或 PostgreSQL 并发锁语义。

在真实 NetBox 4.4.2 开发环境中，启用插件与 `DEVELOPER=True` 后还需运行：

```bash
python manage.py check
python manage.py makemigrations netbox_oa --check --dry-run
python manage.py test netbox_oa.tests
```

上线前还应通过普通申请人和审批人的令牌实测 API 权限、原生页面与并发审批。
数据库迁移已随包提供，安装时执行 `migrate`，无需首次安装时生成迁移。

## 目录

```text
netbox_oa/
  __init__.py        # 插件注册与版本范围
  models.py          # 定义、实例、任务、审批组保留表
  services.py        # 事务、对象权限、状态转换
  api/               # DRF 序列化、查询和审批接口
  migrations/        # 数据库迁移
  forms.py           # 原生表单
  filtersets.py      # 过滤条件
  tables.py          # 原生列表表格
  views.py           # 原生页面与 POST 动作
  navigation.py      # NetBox 导航入口
  templates/         # 详情和状态历史
  tests/             # 可在独立环境或 NetBox 中运行的领域测试
tests/               # 独立测试环境替身（不打包）
```

接口依据 [NetBox 4.4.2 插件模型文档](https://github.com/netbox-community/netbox/blob/v4.4.2/docs/plugins/development/models.md)、
[REST API 文档](https://github.com/netbox-community/netbox/blob/v4.4.2/docs/plugins/development/rest-api.md)
以及 [django-fsm-log 官方说明](https://github.com/jazzband/django-fsm-log) 实现。
