# NetBox OA

面向 **NetBox 4.4.x - 4.6.x**（已验证 4.4.2 及 4.6.2）的轻量工作流插件，Python 包为 `netbox_oa`。
当前兼容范围为 NetBox 4.4.2 至 4.6.x；其他主版本需要单独集成验证。

提供流程定义、草稿申请、顺序审批、拒绝、撤销、审批组校验和 FSM 转换历史。
每个节点由指定 NetBox 用户组中的任意一名有权限的成员处理。
维护工单模块提供四阶段办理、退回补正、转办和加签；通用审批模块仍保留原有顺序审批行为。
当前不包含条件分支、通知推送、EIP 自动同步或对 NetBox 业务对象的自动写回。

## 维护服务工单

工作台入口为 `/plugins/oa/maintenance/`，采用列表与详情双栏布局。
支持待我处理、我发起的、我关注的、全部工单，以及阶段、当前节点、工单号、
服务单位、合同筛选。左右两侧使用同一份阶段与节点状态。
统计包括待办、即将超时、已超时、进行中和本月完成；本月完成按确认收款后的完成时间统计。

四个阶段依次为：工单下单、服务报告、服务结算、付款及收尾。
插件菜单和工作台中的“维护流程定义”入口（`/plugins/oa/maintenance/definition/`）
展示当前代码定义的阶段、节点顺序、处理角色、材料要求、退回位置和付款状态变化。
默认以四阶段流程图展示：实线连接顺序节点，红色虚线连接实际支持的退回位置。
可缩放、隐藏退回路径，点击节点跳到详细规则；图形直接在页面绘制，无外部图形服务依赖。
选择有查看权限的流程配置，可查看七类角色的用户组映射、启用状态及节点时限。
此页面只读，需要维护工单或维护流程配置的查看权限；已启动工单继续使用启动时的流程快照。
流程内置填报、审核、寄送、纸质二审、用印、付款计划、发票验真和收款确认等节点。
EIP 与财务节点由经办人登记外部单号和办理结果，系统不会代替 EIP 审批或执行付款。
"付款申请已提交"、"待财务支付"、"已支付"和"已确认收款"分别记录，确认收款后才能完成工单。

首次使用：

1. 执行数据库迁移和 `collectstatic` 后，在插件菜单打开“维护流程配置”。
2. 配置编号管理员、初审人、部门负责人、上级负责人、印章管理人、服务单位经办组、
   财务经办组，共七种角色；配置节点时限和提前提醒时间，默认 24 小时、提前 4 小时。
3. 为线路主管和角色组配置下表中的 NetBox 对象权限。
4. 发起维护工单，填写合同、服务单位、服务项目及服务位置，保存草稿后启动流程。
   编号自动生成 `GD-年份-六位序号`，编号管理员节点负责确认。
5. 在“公司填写表单”填写并保存维护服务工单，查看打印版并打印；
   乙方材料在“上传附件”对应卡片上传。签字盖章后上传对应版本的扫描件，
   再执行提交、审核通过、登记外部流程已通过或确认办理完成。

| 对象 | 建议权限 |
| --- | --- |
| MaintenancePolicy | 配置管理员 view/add/change；申请人 view |
| MaintenanceOrder | 申请人 view/add/change；所有经办人员对相关工单 view |
| MaintenanceTask | 经办人员对相关任务 view/change；无需 add/delete |
| MaintenanceDocument | 线路主管和经办人员对相关材料 view/add |
| MaintenanceEvent | 相关人员 view |
| MaintenanceLink | 线路主管 view/add；相关人员 view |

示例对象约束：申请人的工单可用 `{"owner_id": "$user"}`；经办组的任务可用
`{"assigned_group_id": 组ID}`。线路主管的任务直接指定本人，可使用
`{"assigned_user_id": "$user"}`。工单、材料与历史的约束需覆盖参与办理的相关工单，
并根据本单位权限范围设置；加入经办组本身不授予对象权限。

任务在事务内先锁工单再锁任务，数据库约束保证一个工单最多有一个待处理节点。
组任务可以领取，领取后其他组员不能继续办理；转办需目标人员具备处理权限，组任务的
转办目标必须属于原经办组。加签在当前审核节点之后插入一个指定人员的审核节点，
组审核节点的加签人员也必须属于原经办组；付款计划在全部加签完成后才标记已提交。
草稿可由申请人或线路主管撤销；进行中的终止仅允许超级管理员填写原因办理。

退回时必须说明原因，系统返回相关填报节点，并重新执行后续审核。涉及材料的补正必须
保存修订表单或上传新附件版本，不能直接重用原版本。已完成阶段的材料不能被覆盖，工单业务信息仅可在草稿或
工单填报节点修改。每次材料提交、领取、转办、加签、退回和办理完成都保留操作历史。

七类材料为：维护服务工单、服务报告、服务结算单、付款通知书、电子发票、合同复印件、
发票验真结果。公司表单与上传附件分开办理：

- 维护服务工单：按样表填写甲方信息、服务项目及服务要求，保存后生成不可覆盖的表单版本。
- 服务报告：甲方填写基础信息，可在草稿、下发服务单位或报告填报节点生成打印表；
  乙方服务内容、费用核算和签章栏保留为空。乙方完成的报告另外上传，甲方表单不计作完整报告。
- 服务结算单、付款通知书、电子发票、合同复印件：上传乙方提供的附件。
- 发票验真结果：由经办人员上传。

打印页面提供“打印 / 另存为 PDF”，使用浏览器 A4 打印；签字盖章栏不自动代签。
表单保留生成时的数据，历史版本可再次查看打印。修改工单基本信息时，已有工单表单同步生成新版本。
签字盖章扫描件单独上传并关联当前原件版本，不能用旧版扫描件满足修订后的材料要求。
原有上传材料继续保留，升级迁移将原纸质/盖章材料归为扫描件并尽可能关联历史电子原件。
部署本次更新需执行数据库迁移（含 `0004_material_forms_and_scans`）及 `collectstatic`。

材料分别记录版本、经办人、材料形态及说明；节点完成时保存所依据的材料版本。
单文件最大 20 MB，附件支持 PDF、DOCX、XLSX、PNG、JPEG，扫描件支持 PDF、PNG、JPEG。
材料保存于受保护目录，下载同时校验材料和所属工单权限，不提供直接文件 URL。
可在插件配置中指定目录：

```python
PLUGINS_CONFIG = {
    "netbox_oa": {
        "maintenance_storage_root": "/opt/netbox/oa-private",
    },
}
```

该目录必须由 NetBox 进程可写，并加入备份。默认位于 `MEDIA_ROOT` 的同级
`netbox_oa_private` 目录，不应通过 Web 服务器静态发布该目录。

合同编号和故障编号作为业务引用保存；关联对象功能可绑定具有 NetBox 对象权限的
合同、故障、站点或设备模型。插件不自动创建其他插件的合同或故障对象。

维护 API 位于 `/api/plugins/oa/maintenance/`：

| 资源或动作 | 方法 |
| --- | --- |
| `policies/` | GET / POST |
| `policies/{id}/configure/` | POST |
| `orders/` | GET / POST |
| `orders/{id}/start/`、`withdraw/`、`terminate/` | POST |
| `tasks/`、`tasks/{id}/` | GET |
| `tasks/{id}/complete/`、`return/`、`claim/`、`transfer/`、`countersign/` | POST |
| `documents/` | GET / POST（文件上传使用 multipart） |

材料 API 返回 `kind`（form/attachment/scan）、`form_data` 和 `source_document`，
扫描件上传须提供当前原件的 `source_document_id`，维护服务工单电子版通过页面表单生成。

填报付款计划的 `complete` 请求须包含 `payment_amount`、`period_start` 和 `period_end`；
外部节点须包含 `external_reference`。转办和加签须包含 `user_id` 与 `comment`。
普通 API 不允许直接修改工单、任务状态或付款结果。

升级后执行 `manage.py migrate`、`manage.py collectstatic --no-input`，然后重启 Web 和 worker。
新增迁移为 `0003_maintenancepolicy_maintenancegroupreservation_and_more`，不改写已有通用审批数据。

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
真实 NetBox 的模板集成或 PostgreSQL 并发锁语义。维护模块另包含 Django HTTP 页面、
CSRF、文件下载和真实 DRF 接口测试；DRF 测试使用精简 NetBox API 适配器，仍需在
部署环境验证 NetBox 的实际对象权限后端和令牌认证。

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
