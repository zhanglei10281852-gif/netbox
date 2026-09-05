# Webhook 多签名密钥（密钥轮换）实施计划

## 需求要点（来自用户）

1. 一个 Webhook 可维护多个带**稳定标识（key_id）**的签名密钥，且明确**一个主密钥**。
2. 轮换重叠期：每次发送对**最终请求体**用每个启用的密钥分别生成 HMAC 签名，接收方可按 key_id 区分并接受新旧密钥；同时继续发送由主密钥生成的现有 `X-Hook-Signature` 头以兼容旧接收端。
3. 密钥退役后，**新入队**事件不得携带其签名。
4. **入队时固定**本次发送所用密钥集合（快照随 RQ 任务参数持久化）：排队期间配置变化、任务自动重试都不得改变该事件的签名身份。
5. 创建/启用/切换主密钥/退役等操作必须：拒绝空标识、重复标识、无主密钥等不完整状态；**不留下部分更新**（事务性）。
6. 升级兼容：已有单 `secret` 的 Webhook 迁移后签名结果与原来完全一致；未配置 secret 的 Webhook 仍不签名。

## 仓库调研结论

- Webhook 模型：[models.py](file:///f:/swe/090501/project-01/netbox/extras/models/models.py#L209-L407)（`extras` 应用），含 `secret` CharField(blank=True, max_length=255)。
- 发送逻辑：[webhooks.py](file:///f:/swe/090501/project-01/netbox/extras/webhooks.py#L35-L115)。`generate_signature(body, secret)` = HMAC-SHA512 hex；发送时在 `requests` 准备请求后对 `prepared_request.body` 签名，设置 `X-Hook-Signature`；`webhook.secret == ''` 时不签名。
- 入队：[event_rules.py](file:///f:/swe/090501/project-01/netbox/extras/event_rules.py#L33-L54) `WebhookAction.enqueue()` 构造 params 后 `rq_queue.enqueue('extras.webhooks.send_webhook', **params)`；参数随任务 pickle 持久化，RQ 重试复用同一组参数（`get_rq_retry()`）。事件在请求事务提交后由 `process_event_rules()` 处理。
- 编辑视图为通用 `ObjectEditView`（[object_views.py](file:///f:/swe/090501/project-01/netbox/netbox/views/generic/object_views.py#L296-L309)），其 POST 已将 `form.save()` 包在 `transaction.atomic()` 中；表单校验在 `form.is_valid()` 完成。
- Webhook 相关面：API 序列化器 [serializers_/events.py](file:///f:/swe/090501/project-01/netbox/extras/api/serializers_/events.py#L60-L69)（含 `secret` 字段，GET 可读）；表单 [model_forms.py](file:///f:/swe/090501/project-01/netbox/extras/forms/model_forms.py#L571-L593)、bulk_edit、bulk_import、filtersets、tables、panels（`WebhookHTTPPanel.secret = TextAttr`）、GraphQL `WebhookType`（strawberry auto）。
- 迁移：extras 最新为 `0144_customfield_status.py`；仓库有手写 RunPython 数据迁移先例（如 `0108_convert_reports_to_scripts.py`）。
- 测试：发送/入队测试在 [test_event_rules.py](file:///f:/swe/090501/project-01/netbox/extras/tests/test_event_rules.py)（setUp 直接用 `Webhook(secret=...)` 建对象；`test_send_webhook` 断言 `X-Hook-Signature`）；test_signals.py、test_api.py、test_views.py 有相关引用。
- 环境限制：本机 Python 3.11 / Django 5.1，NetBox 4.6 要求 Python 3.12 / Django 6.1，**无法在此环境运行 Django 测试或 makemigrations**；迁移文件按 Django autodetector 输出格式手工编写（与 NetBox 仓库手写数据迁移的惯例一致），最终用 `py_compile`/ruff 做静态校验，并在交付说明中要求用户运行 `makemigrations --check` 与测试套件复核。

## 设计方案

### 数据模型（`extras/models/models.py`）

新增 `WebhookSecret(models.Model)`（非 ChangeLoggedModel，避免密钥进入变更日志）：

- `webhook`：FK→Webhook，`on_delete=CASCADE`，`related_name='secrets'`
- `key_id`：CharField(max_length=100)，稳定标识，仅允许 `[A-Za-z0-9_-]+`（保证可安全出现在签名头中）
- `secret`：CharField(max_length=255)，HMAC 密钥
- `status`：CharField(choices=`WebhookSecretStatusChoices`, default=enabled)
- `is_primary`：BooleanField(default=False)
- Meta：`ordering = ('key_id',)`；约束：`UniqueConstraint(fields=['webhook','key_id'], ...)` 与部分唯一约束 `UniqueConstraint(fields=['webhook'], condition=Q(is_primary=True), ...)`（DB 层保证每个 webhook 至多一个主密钥）

新增 `WebhookSecretStatusChoices`（`extras/choices.py`）：

- `enabled`（启用：对新事件签名；绿色）
- `disabled`（停用：暂存/暂停，不签名，可重新启用；灰色）
- `retired`（退役：终态，不签名，不可改回/编辑，只能删除；红色）

Webhook 模型：**移除 `secret` 字段**；新增方法：

- `get_signing_keys_snapshot()`：返回启用中密钥的快照 `[{'key_id', 'secret', 'is_primary'}, ...]`（按 key_id 排序），入队与旧任务回退使用。
- `sync_secrets(keys)`：在调用方事务内对账（先 `secrets.update(is_primary=False)` 避免部分唯一约束瞬态冲突，再 `update_or_create`，最后删除未出现的行）。

模块级校验函数 `validate_signing_secrets(webhook, keys)`（表单与 API 共用），规则：

1. 空列表 → 合法（不签名）。
2. key_id 非空、字符集合法、≤100；secret 非空、≤255；status 合法。
3. 集合内 key_id 不得重复。
4. 有密钥时恰好一个 `is_primary=True`，且主密钥 status=enabled；退役行不得为主密钥（报错提示先切换主密钥）。
5. 终态：库中已 retired 的行，提交值不得改其 status/secret（只能原样保留或整行删除）。
6. 校验失败抛 `django.core.exceptions.ValidationError({'secrets': [...]})`；所有写操作在 `transaction.atomic()` 内执行 → 无部分更新。

迁移（3 个，顺序执行）：

- `0145_webhooksecret.py`：CreateModel（字段/约束与模型一致）。
- `0146_webhook_secrets_populate.py`：RunPython：为每个 `secret != ''` 的 Webhook 创建 `WebhookSecret(key_id='default', secret=<旧secret>, status='enabled', is_primary=True)`；反向：把主密钥写回 `secret` 列（尽力而为）。
- `0147_remove_webhook_secret.py`：RemoveField `webhook.secret`。

### 入队快照（`extras/event_rules.py`）

`WebhookAction.enqueue()` 的 params 增加 `'signing_keys': action_object.get_signing_keys_snapshot()`。快照在入队时（事件 flush、事务提交后）生成并随任务 pickle；退役/停用/新增密钥对已入队任务无影响；重试复用同一份 kwargs。

### 发送签名（`extras/webhooks.py`）

- `send_webhook(..., signing_keys=None)`：若 kwargs 无 `signing_keys`（升级前已入队的旧任务），回退为 `webhook.get_signing_keys_snapshot()`（等价旧代码“发送时实时读 secret”的行为）。
- 请求 prepare 之后（签名对象为最终 `prepared_request.body`，与现状一致）：
  - 对快照中每个密钥计算 `generate_signature(body, secret)`（算法不变，HMAC-SHA512 hex）。
  - `X-Hook-Signature` = 主密钥签名（迁移后主密钥 secret 与旧 secret 相同 → 头值逐字节不变）。
  - 新增 `X-Hook-Signatures`：逗号分隔的 `key_id=hexdigest` 列表（key_id 字符集受限，解析无歧义），包含全部启用密钥（含主密钥）。
  - 快照为空 → 不设置任何签名头（与旧“无 secret 不签名”一致）。

### REST API（`extras/api/serializers_/events.py`）

- 新增 `WebhookSecretSerializer(serializers.Serializer)`：`key_id`、`secret`、`status`（ChoiceField，默认 enabled）、`is_primary`（默认 False）。
- `WebhookSerializer`：移除 `secret`，新增 `secrets = WebhookSecretSerializer(many=True, required=False)`（GET 可读 secret，与旧字段行为一致）。
- `create()`/`update()`：弹出 `secrets`，`transaction.atomic()` 内先存 webhook，再调用共用校验 + `sync_secrets()`；django ValidationError 转为 DRF ValidationError（400，按 secrets 字段返回）。PATCH 不带 `secrets` 时密钥保持不变。

### UI（表单/面板/过滤器/表格）

- 新增 `WebhookSecretsWidget`（自定义模板 `templates/extras/widgets/webhook_secrets.html`）+ `WebhookSecretsField`，置于 [model_forms.py](file:///f:/swe/090501/project-01/netbox/extras/forms/model_forms.py)：表格行渲染 主密钥单选 / key_id / secret / 状态（enabled/disabled/retired）/ 删除勾选；已有行在前、附 3 个空行（无需 JS）；退役行只读并带隐藏输入与删除框。`value_from_datadict` 收集并行数组为行 dict 列表（丢弃全空行、删除行）。
- `WebhookForm`：字段集用 `secrets` 替换 `secret`；`__init__` 中给初值；`clean()` 调共用校验；`save()` 在通用视图的事务内对账子密钥。
- `WebhookBulkEditForm`：移除 secret 字段。
- `WebhookImportForm`：`secret` 改为声明式表单字段（保留旧 CSV 列兼容），`save()` 时若提供则创建 `key_id='default'` 的主密钥。
- FilterSet/Table：移除 `secret`。
- 面板：`WebhookHTTPPanel` 移除 secret；新增 `WebhookSecretsPanel`（自定义模板 `templates/extras/panels/webhook_secrets.html`，展示 key_id/状态徽章/主密钥/secret），挂到 Webhook 详情页布局。
- GraphQL：`WebhookType` 的 exclude 增加 `'secrets'`（密钥不进 GraphQL）。

### 文档（更新既有文件）

- [docs/models/extras/webhook.md](file:///f:/swe/090501/project-01/docs/models/extras/webhook.md)：Secret 一节替换为“签名密钥”（多密钥、key_id、主密钥、状态、轮换流程、两个签名头）。
- [docs/integrations/webhooks.md](file:///f:/swe/090501/project-01/docs/integrations/webhooks.md)：新增“请求签名/密钥轮换”章节，说明 `X-Hook-Signature`（主密钥，兼容）与 `X-Hook-Signatures`（`key_id=hex` 列表）格式及接收端验签建议。

## 实施步骤（依赖顺序）

1. `extras/choices.py`：新增 `WebhookSecretStatusChoices`。
2. `extras/models/models.py`：新增 `WebhookSecret`、`validate_signing_secrets()`；Webhook 移除 `secret`、新增快照/对账方法；`__all__` 导出。
3. 手写迁移 0145/0146/0147。
4. `extras/event_rules.py`：入队快照；`extras/webhooks.py`：多密钥签名头 + 旧任务回退。
5. API 序列化器：嵌套 secrets + 事务化 create/update。
6. 表单（model_forms widget/field、bulk_edit、bulk_import）、filtersets、tables、panels + 两个模板、GraphQL exclude。
7. 更新文档。
8. 更新/新增测试（见下）。
9. 静态校验：`python -m py_compile` 全部改动文件；`ruff check`；人工核对迁移字段与模型一致。

## 测试计划

更新既有引用（`secret=` 构造改为创建 WebhookSecret 行）：test_event_rules.py（setUp 3 个 webhook、`test_send_webhook` 断言）、test_signals.py、必要时 test_api/test_views 夹具。

新增测试：

- 模型/校验（test_models.py 或 test_event_rules.py）：重复 key_id、空 key_id、非法字符、空 secret、无主密钥、多个主密钥、主密钥非 enabled、退役主密钥被拒、retired 终态不可改；空密钥集合合法。
- 快照：仅含 enabled；disabled/retired 不入快照；快照结构正确。
- 发送：多密钥时 `X-Hook-Signatures` 含每个启用密钥且签名可独立验签；`X-Hook-Signature` 等于主密钥签名且等于 `generate_signature(body, 'default-secret')`（迁移等价性）；无密钥时无任何签名头。
- 入队冻结：入队后退役旧密钥/新增新密钥，执行任务仍只携带快照内密钥（旧密钥签名仍在、新密钥缺席）；任务 kwargs 中 `signing_keys` 固定（重试身份不变）。
- API：带嵌套 secrets 创建/更新成功；无主密钥/重复 id → 400 且库中无部分写入；轮换（加新密钥→切主→退役旧）全流程；GET 不再返回 `secret` 字段。
- 表单：POST 含密钥行创建成功；非法提交返回表单错误且数据不变。

## 风险与处理

- **无法在本机运行测试/makemigrations**（Py3.11 vs 要求 3.12）：迁移按 autodetector 格式精写；交付后请用户运行 `python manage.py makemigrations --check`（应无变更）、`python manage.py migrate`、`python manage.py test extras` 复核。
- **升级瞬间已排队的旧任务**：kwargs 无 `signing_keys` → 回退实时读取当前密钥（与旧行为一致），不会报错或丢签名。
- **切换主密钥时部分唯一约束瞬态冲突**：对账前先清空 `is_primary`，事务提交时状态合法。
- **密钥外泄面**：WebhookSecret 不做变更日志、不进 GraphQL；API/UI 可读性与旧 `secret` 字段保持一致（原本即明文可读）。
- **CSV 批量导入旧文件**：保留 `secret` 列映射为 `default` 主密钥，避免旧导入流程断裂。
