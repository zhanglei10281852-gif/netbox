# Idempotency-Key 功能 - 独立审查记录

## 审查范围

用户目标：NetBox REST API 写请求（POST/PUT/PATCH/DELETE）支持 Idempotency-Key，使控制器在代理超时/断连后可安全重试：同键同语义重放原结果且无重复副作用；同键不同语义 409；并发单执行者；5xx 不留阻塞占位；配置化保留期回收。

实现工件（仓库根：f:\swe\090501\project-02）：
- netbox/netbox/api/idempotency.py（核心：指纹、作用域、执行者/等待者/重放、schema hook）
- netbox/netbox/api/exceptions.py（IdempotencyKeyConflict/InProgress）
- netbox/netbox/api/viewsets/__init__.py（BaseViewSet.dispatch 接入）
- netbox/core/models/idempotency.py + netbox/core/migrations/0027_idempotencykey.py
- netbox/core/models/__init__.py、netbox/core/jobs.py（housekeeping 回收）
- netbox/netbox/settings.py、netbox/netbox/configuration_example.py（配置 + schema hook 注册）
- netbox/netbox/tests/test_api_idempotency.py（17 测试）
- docs/configuration/miscellaneous.md、docs/integrations/rest-api.md（文档）

运行测试（仓库根的 netbox/ 目录下）：
- 需 Python 3.12+ 环境与本机 PostgreSQL/Redis。本环境使用 Python 3.13 venv（C:\Users\czxy4\AppData\Local\Temp\nbvenv313，已装 requirements，psycopg[binary]）。
- `$env:NETBOX_CONFIGURATION='netbox.configuration_testing'; $env:PYTHONPATH=(Get-Location).Path` 后 `python manage.py test netbox.tests.test_api_idempotency --noinput`
- ruff：`python -m ruff check <changed files>`（仓库根）

## Checkpoints

### CP-R1: 启用条件与作用域（AC-1, rule）
- [ ] 仅已认证的不安全方法（POST/PUT/PATCH/DELETE）携带合法 Idempotency-Key 时介入；无键/GET/OPTIONS/405 handler/未认证不介入
- [ ] 作用域 = 用户 + 方法 + 规范化路径（去尾部斜杠）+ 键
- 证据：

### CP-R2: 首次完成后保存指纹与最终响应（AC-2, rule）
- [ ] 记录保存指纹（body+显著查询参数）、状态码、渲染 body、内容类型、Location/ETag
- [ ] 4xx 终端响应保存；状态 complete
- 证据：

### CP-R3: 同键同语义重放且无重复副作用（AC-3, rule）
- [ ] 重放状态码/body/内容类型/Location/ETag 一致，附 Idempotency-Replayed: true
- [ ] 不执行 handler：无第二次写库、无新 ObjectChange、不 flush 事件、不二次入队 background job（202 重放）
- 证据：

### CP-R4: 同键不同语义冲突（AC-4, rule）
- [ ] body 不同或 background 等写语义参数不同 → 409 且不执行
- [ ] fields/omit/brief/format 差异不冲突
- 证据：

### CP-R5: 并发单执行者（AC-5, rule）
- [ ] 真实并发（多连接/事务）下恰好一个执行者，其余等待后重放同一结果
- [ ] 执行者回滚时等待者可接管
- 证据：

### CP-R6: 失败与认证流程（AC-6, rule）
- [ ] 5xx/未捕获异常：占位随事务回滚释放，同键重试可重新执行
- [ ] 401/403 认证失败发生在介入之前，不创建记录
- [ ] 非法键（空/超长/控制字符）→ 400
- [ ] 无键请求行为与原生 DRF 完全一致（dispatch 镜像正确性）
- 证据：

### CP-R7: 作用域隔离（AC-7, rule）
- [ ] 不同用户同键互不影响；路径不同互不影响
- 证据：

### CP-R8: 回收与开关（AC-8, rule）
- [ ] prune_expired 按保留期删除 complete 记录；0/None 永久保留；SystemHousekeepingJob 调度
- [ ] IDEMPOTENCY_KEY_ENABLED=False 时完全穿透
- 证据：

### CP-R9: 质量一致性（AC-9, rubric, threshold >=4）
- [ ] 接入点/事务/异常翻译复用既有机制；无新依赖；ruff 通过；迁移由 makemigrations 生成；文档与 OpenAPI 齐备
- 证据：

## 审查结果

审查人：独立代码审查（独立于实施）。审查日期：2026-09-05。以下结论均基于本人通读代码与实际运行结果，未引用实施方自证作为证据。

### 运行验证汇总（均在 f:\swe\090501\project-02\netbox 下，NETBOX_CONFIGURATION=netbox.configuration_testing）

1. `python manage.py test netbox.tests.test_api_idempotency --noinput` → **Ran 17 tests，全部 OK**。
2. `python -m ruff check`（idempotency.py、viewsets/__init__.py、models/idempotency.py、core/jobs.py、test_api_idempotency.py、0027 迁移、api/exceptions.py）→ **All checks passed**。
3. `python manage.py test netbox.tests.test_api_background core.tests.test_jobs --noinput` → **Ran 42 tests，全部 OK**（background/job 回归）。
4. 本人另写临时验证测试（8 个，验证后已删除）覆盖官方测试未覆盖的路径：412 ETag 回放、multipart body 预读、404 回放、插件式原生 HttpResponse 端点、路径隔离、执行者 5xx 后等待者接管、55P03 锁超时→409→提交后重放、并发异指纹→409。结果 **7 个通过，1 个复现出下述问题 1（AttributeError）**。其中接管测试在 DB 层实测到时序：执行者 A 取得占位 pk=1（T+2.07s）→ 等待者 B 的 INSERT 阻塞 → A 503 回滚后 B 在 T+4.08s 取得 pk=2 成为执行者（A 返回 503、B 返回 201 且无重放标记、对象只创建一次）。
5. `manage.py spectacular` 生成成功，schema 中 Idempotency-Key 参数出现 953 处；抽查 `/api/dcim/regions/` 与 `{id}/`：**GET 无该参数，post/put/patch/delete 均有**。

### CP-R1: 启用条件与作用域（AC-1）—— 通过

- `IdempotencyExchange.engage()`（idempotency.py:147-166）顺序：ENABLE 开关 → 方法 ∈ {POST,PUT,PATCH,DELETE} → 非 405 handler → `request.user.is_authenticated` → `validate_key()`。它在 `BaseViewSet.initial()`（DRF 认证/权限/限流 + NetBox 的 queryset.restrict）之后调用（viewsets/__init__.py:103-115），因此认证/权限/限流拒绝不创建任何记录；官方 `test_authentication_failure_not_recorded`（无效 token 401/403 且记录数 0、随后同键合法请求成功）通过。
- 无键请求 `validate_key(None)→None`，engage 返回 None，且在指纹/任何 DB 访问之前返回，满足 NFR-1（无键零额外查询）。GET/HEAD/OPTIONS 与 405 不介入（UNSAFE_METHODS + `method_not_allowed` 标识；405 分支由 handler 解析同一性判定，`handler is self.http_method_not_allowed`，与 DRF views.py:517-521 解析逻辑一致）。
- 作用域 = sha256(user_pk, method, `normalize_path`(rstrip 尾部斜杠), key)（idempotency.py:62-70, 142）；官方 `test_scope_isolated_by_user`（两用户同键各建对象、2 条记录）与本人的路径隔离测试（collection POST 与 detail PATCH 同键互不影响）均通过。BASE_PATH 是 request.path 的固定前缀，部署内一致，不影响隔离。

### CP-R2: 首次完成后保存指纹与最终响应（AC-2）—— 通过

- 执行者在同一事务内先 INSERT 占位（status=in-progress），handler 完成后 `finalize_response` → `render()` → `_store()` 写 status/body/content_type/Location/ETag，状态置 complete（idempotency.py:175-204, 242-255）。官方 `test_first_request_records_and_retry_replays` 断言记录 method/path/status=complete/response_status=201/Location 与响应一致、body 含对象 PK，通过。
- 4xx 终端响应（400/404/412 等）走 `_store`（仅 `>=500` 才 set_rollback，idempotency.py:199-204）：官方 400 回放测试、本人 404 回放（body 一致）与 **412 回放**（If-Match 失配返回 412+ETag，重放带 `Idempotency-Replayed: true`、ETag 与 body 逐字节一致、对象未被修改）均通过。
- 204 空响应：DRF 渲染空结果时删除 Content-Type（rest_framework/response.py:82-83），存储 content_type=''；回放时显式删除 HttpResponse 默认 Content-Type（idempotency.py:283-285）；官方 DELETE 204 重放测试通过。
- ETag/Location 白名单回放：官方断言 Location；本人断言 ETag。Allow/Vary 默认头在 dispatch 的 finalize_response 中补回（DRF views.py:159-164），重放响应头完整。

### CP-R3: 同键同语义重放且无重复副作用（AC-3）—— 通过

- 重放走 `_build_replay`（idempotency.py:275-289），**不调用 handler**：官方测试断言重复 POST 仅 1 个对象、ObjectChange=1（create/update/delete 三种方法各断言）、background 202 重放 Job 计数仍为 1 且 job id 相同、`flush_events` 在两次请求中只被调用 1 次（重放时 events_queue 为空，context_managers.py:27 的 `if events :=` 守卫不触发 flush）。
- changelog/event 信号机制：ObjectChange/event 由模型信号在 handler 内产生（core/signals.py:87-155），重放无 handler 即无信号；`IdempotencyKey` 本身无 `to_objectchange`，信号接收器在 core/signals.py:94 提前返回，不产生 changelog/event。
- background job 入队安全：`Job.enqueue` 的 RQ push 挂在 `transaction.on_commit`（core/models/jobs.py:339-344），有键时随幂等外层事务提交后触发——Job 行先提交、push 后发生，worker 不会读到未提交行；202 重放不再次 enqueue（官方测试断言）。

### CP-R4: 同键不同语义冲突（AC-4）—— 通过

- 等待者 23505 后 `_replay_or_conflict` 比对 fingerprint，不同则 `IdempotencyKeyConflict`（409）（idempotency.py:257-273）；执行者已提交场景官方 `test_same_key_different_body_conflicts`（409、无新对象、首个对象仍在）与 `test_background_param_is_significant_and_replays_202`（background vs 同步 → 409）通过。
- 本人另测**并发**异指纹：A 持占位执行 payload-X，B 同键 payload-Y 阻塞至 A 提交后收到 409（detail 含 "different request payload"），Y 未建对象——通过。
- 指纹（idempotency.py:73-95）= 原始 body 字节 + 排序后的查询参数（多值用 `lists()` 保序），排除 brief/fields/format/omit；官方 `test_response_shaping_query_params_do_not_conflict`（?fields 与 ?omit 互换正常重放）通过。

### CP-R5: 并发单执行者（AC-5）—— 通过

机制逐项推演 + 实证，结论成立：

- **阻塞模型成立**：执行者 INSERT 未提交占位行，同键并发 INSERT 在 PostgreSQL 唯一索引上阻塞等待先到事务；提交 → 等待者收 23505；回滚 → 等待者 INSERT 成功自动接管。官方 `IdempotencyKeyConcurrencyTests`（4 线程 Barrier 真并发：仅 1 对象、响应体全同、恰 1 个无重放标记）通过；本人的接管测试在 DB 时序上直接观测到阻塞→接管（B 的 INSERT 从 T+2.06 阻塞至 T+4.08 A 回滚后取得新 pk）。
- **锁超时 55P03 判定**：等待者 INSERT 超时（`SET LOCAL lock_timeout`，idempotency.py:291-294）抛 55P03 → `IdempotencyKeyInProgress`（409，提示稍后重试）；`_sqlstate()` 遍历 `__cause__/__context__` 链取 psycopg 的 sqlstate（idempotency.py:120-128）。本人实测 `IDEMPOTENCY_KEY_LOCK_TIMEOUT=1`：等待者 1s 后收到 409 且 detail 含 "still being processed"，执行者随后正常 201，再重试重放同一 201。
- **无死锁**：等待者在 INSERT 前仅执行 SET LOCAL，不持有任何执行者需要的锁；执行者 reset lock_timeout 后其 handler 的锁等待不受 60s 限制（idempotency.py:186, 296-298）。
- **handler 自身 IntegrityError 不误判**：`self.acquired` 在 INSERT 成功后才置 True（idempotency.py:185），外层只在 `not self.acquired and sqlstate=='23505'` 才走重放（idempotency.py:208）；handler 内的 23505 会按原生路径传播（DRF 不处理 → 500），与无键行为一致且占位回滚。
- **多等待者级联**：执行者回滚后，多个等待者中一个 INSERT 成功成为新执行者，其余继续阻塞/重放，串行接管，仍恰好一个执行者（机制推演）。

### CP-R6: 失败与认证流程 / 无键路径等价性（AC-6）—— 通过

- **5xx 释放**：返回型 5xx Response（如 ServiceUnavailable 503）→ `set_rollback(True)` 回滚整个占位事务（idempotency.py:199-202）；未捕获异常 → 异常冲出 atomic 回滚（acquired=True 不触发重放，idempotency.py:205-210）。官方 `test_server_error_releases_placeholder`（无 worker 时 503、记录数 0、worker 可用后同键重试 202）与本人 takeover 测试（503 后等待者接管成功）通过。
- **异常翻译等价**：有键路径 handler 异常先经 `exception_to_response`（ProtectedError/RestrictedError→409、AbortRequest→400，viewsets/__init__.py:267-291）再经 `handle_exception`（DRF 400/403/404/429 等）；与无键路径 NetBoxModelViewSet.dispatch 的两个 except（viewsets/__init__.py:244-265）响应体、日志（同一 logger、同一消息）一致。ETagMixin.handle_exception 的 412 ETag 行为在有键路径经 `view.handle_exception` 保留（本人 412 测试实证）。
- **dispatch 镜像核对**：BaseViewSet.dispatch（viewsets/__init__.py:89-125）与 DRF 3.18 APIView.dispatch（rest_framework/views.py:490-529）逐行对应：args/kwargs 赋值、initialize_request、`self.headers`、initial、handler 解析（http_method_names + getattr 默认值）、try/except→handle_exception、finalize_response。无键路径执行的是与原生完全相同的语句序列。
- **非法键**：空/纯空白/超长（>255）/含控制字符 → ValidationError 400（idempotency.py:98-117），官方 `test_malformed_key_rejected`（''、256 字符、含换行均 400、无记录）通过。
- **render 幂等**：执行者路径 finalize 被调用两次（run 内一次、dispatch 一次），SimpleTemplateResponse.render 有 `_is_rendered` 守卫（django/template/response.py:113），第二次为 no-op；Vary 头 pop 后第二次不重复 patch。无副作用。
- **body 预读安全**：指纹读取 `request._request.body`，Django 缓存原始字节；DRF 在 `_read_started=True` 后经 `io.BytesIO(self.body)` 重放流（rest_framework/request.py:311-314），JSON 与 multipart 解析均不受影响（官方 JSON 全套 + 本人 multipart 创建/重放测试通过）。

### CP-R7: 作用域隔离（AC-7）—— 通过

- 不同用户同键：官方 `test_scope_isolated_by_user`（各 201、均无重放标记、2 条记录）通过；不同方法/路径：作用域哈希含 method 与 rstrip 路径，本人路径隔离测试通过；指纹内不包含用户但 scope 包含，跨用户重放/冲突均不可能。

### CP-R8: 回收与开关（AC-8）—— 通过

- `IdempotencyKeyManager.prune_expired`（core/models/idempotency.py:15-39）仅删 `status=complete 且 created < cutoff`；0/None 不删；官方 `test_prune_expired_records`（过期删 1、未过期留、retention=0 返回 0）与 `test_housekeeping_job_prunes_records`（直接调 SystemHousekeepingJob.prune_idempotency_keys，core/jobs.py:177-191，记录清零）通过。
- in-progress 行只在执行者事务内存在、提交即为 complete，进程崩溃由连接断开回滚清理，故不参与回收——设计正确。
- `IDEMPOTENCY_KEY_ENABLED=False` 时 engage 第一行即返回 None（idempotency.py:155-156），官方 `test_feature_disabled`（带键写 201、无记录、无重放标记）通过。三项设置 settings.py:162-164 getattr 默认值（True/86400/60）与 configuration_example.py:162-174 一致。

### CP-R9: 质量一致性（AC-9）—— 通过（评分 4/5）

- 复用既有机制：dispatch 层接入、exception_to_response 翻译入口、SystemHousekeepingJob 回收、settings getattr 配置模式、_netbox_private 内部模型（无公开 ObjectType/changelog/event）；无新依赖；迁移 0027 序号正确且由 makemigrations 生成；ruff 通过；配置文档（miscellaneous.md 三项）与 REST API 指南（rest-api.md 幂等写入章节 + 两个响应/请求头参考）齐备；OpenAPI hook 注册于 SPECTACULAR_SETTINGS.POSTPROCESSING_HOOKS 且生成验证通过。
- AsyncAPIJob worker 直接调用 action（netbox/jobs.py:388），绕过 dispatch，不受影响（test_api_background 回归通过）。
- on_commit 语义排查：全部调用点（Job.enqueue RQ push、CustomFieldPurgeJob、dcim 级联改名、search deferred flush、config context cache）在有键时推迟到外层原子提交后执行，均为"提交后触发"语义，不改变正确性；RQ push 晚于 Job 行提交，顺序更稳。
- 扣 1 分原因：见问题 1（对非 DRF Response 的兼容性假设）。

### 发现的问题

**问题 1（major）：执行者路径无条件调用 `response.render()`，返回原生 HttpResponse/StreamingHttpResponse 的 unsafe 端点在带键时 500**
- 位置：netbox/netbox/api/idempotency.py:197（`response.render()`），配合 195 行 finalize_response。
- 复现：本人临时测试用 monkeypatch 让 RegionViewSet.create 返回 `django.http.HttpResponse(status=200)`：无键请求正常 200；带 Idempotency-Key 时 `AttributeError: 'HttpResponse' object has no attribute 'render'`，异常冲出 run()/dispatch → 500。此时 handler 的非 DB 副作用（Redis/RQ 操作、外部 HTTP 调用等）已经发生，事务回滚无法撤销；客户端收到 500 后按幂等语义重试会再次触发副作用。
- 影响面评估：NetBox core 内 unsafe 端点均返回 DRF Response；core 中返回原生 HttpResponse 的 POST（BackgroundTaskViewSet 的 delete/requeue/enqueue/stop，core/api/views.py:253-288）其视图集继承 DRF `viewsets.ViewSet` 而非 NetBox BaseViewSet，不经过该 dispatch，故 core 不受影响。但需求 Constraints 明确"插件视图集继承 BaseViewSet 自动获得能力且无需改动"，插件的写类 @action 返回 HttpResponse/StreamingHttpResponse（如下载/导出类）即触发本问题。
- 建议修复：渲染前守卫，仅对 DRF Response 渲染并从其取 content：
  ```python
  if hasattr(response, 'render'):
      response.render()
  body = response.content or b''  # HttpResponse 也有 content
  ```
  （_store 中 `response.content` 对两类响应均可用；Content-Type 读取同理。）

**问题 2（minor）：`path` 字段 max_length=255，超长 URL 带键时 22001 → 500**
- 位置：core/models/idempotency.py:70-72；INSERT 失败 SQLSTATE 22001 不属于 23505/55P03，会按未捕获异常传播为 500（仅影响带键的超长路径请求，无键路径无此问题）。scope_hash 才是权威匹配键，path 仅为审计信息。建议：path 改 TextField 或在存储时截断至 255。

**问题 3（minor，加固）：返回型 5xx 回滚后事件队列的理论 flush 窗口**
- 位置：idempotency.py:199-202 与 netbox/context_managers.py:22-28。若 handler 先经信号入队了事件、再以"返回 5xx Response"（非抛异常）结束，run() 回滚 DB（ObjectChange 行一并回滚），但 event_tracking 在 yield 正常返回后仍会 flush 线程队列中的事件，可能对已回滚的写入触发 webhook/event rule。当前 NetBox 内 5xx Response 路径（ServiceUnavailable，无 worker）发生在任何写入之前，未发现可达场景；建议在 set_rollback 分支同时清空 events 队列（复用 clear_events 机制）以闭环。

**问题 4（minor，文档/易用性）**：重放以执行者的 Content-Type/body 为准，重试时 Accept 不同仍返回原渲染格式（行为正确，但 finalize 会补 `Vary: Accept`，略有不一致）；multipart/form-data 重试若客户端重新生成 boundary，body 字节不同会被判 409（JSON API 场景无影响），建议在 rest-api.md 提示。

**问题 5（minor，可读性）**：run() 中 `finalize_response` 被调用两次（run 内 + dispatch 内），虽已验证幂等无副作用，建议在 run() 内注释说明，或改为 run() 返回未 finalize 的响应由 dispatch 统一 finalize（重放路径已是该模式），减少后续维护者误判。

### 总体结论

**pass**。

AC-1 至 AC-9 全部满足：并发互斥/接管/锁超时、5xx 释放、重放副作用抑制（对象/ObjectChange/event/background job）、指纹冲突、作用域隔离、认证穿透、回收与开关、无键路径与 DRF 原生等价、OpenAPI/文档/迁移/测试齐备，均经代码推演与运行实证（官方 17 测试 + 回归 42 测试 + 本人 8 项独立验证测试，ruff 通过）。

合并前建议修复问题 1（major，一行守卫级别的改动，消除插件端点 500 与重复副作用风险）；问题 2-5 为 minor 加固，可后续处理。

### 修复复核（实现方）

审查后所有问题均已修复并重新验证：

1. **问题 1（major）已修复**：run() 中 `response.render()` 增加守卫，仅对带 `.render()` 的 DRF Response 渲染；`StreamingHttpResponse`（streaming 且无法捕获回放）改为回滚释放占位，不记录不可重放的结果。`_store` 统一读 `response.content`（DRF 渲染后与原生 HttpResponse 均可用）。新增回归测试 `test_plain_http_response_handled`（monkeypatch 视图返回原生 Django HttpResponse：带键请求 201，重放带 Idempotency-Replayed 且 body 一致）。
2. **问题 2 已修复**：`IdempotencyKey.path` max_length 255→2000（该列不参与索引，scope_hash 仍为权威键）；迁移删除后重新 makemigrations 生成（FK 行长行已按 ruff 换行）。
3. **问题 3 已加固**：返回型 5xx 分支在 `set_rollback` 后同时 `clear_events.send(sender=view)`，丢弃队列事件，与事务回滚语义闭环。
4. **问题 4 已补文档**：rest-api.md Idempotent Writes 章节增加注释：指纹基于原始 body（multipart boundary 变化会判 409，JSON 客户端不受影响）；重放返回首次请求的 content type/Location/ETag，不受重试 Accept 影响。
5. **问题 5 已加注释**：run() 内 finalize_response 处补充"dispatch 会再次调用且幂等"的说明。

复核结果（修复后重跑）：

- `python manage.py test netbox.tests.test_api_idempotency netbox.tests.test_api_background` → Ran 37 tests，OK（18 个幂等测试，含新增 1 个）。
- `ruff check` 全部改动文件 → All checks passed。
- `makemigrations --check --dry-run` → No changes detected。

**最终结论：pass。**

