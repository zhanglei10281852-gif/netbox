# NetBox REST API 幂等键（Idempotency-Key）支持 - 实施计划

## Task 1: 新增幂等键配置项
- **Status**: `completed`
- **Priority**: high
- **Depends On**: None
- **Description**:
  - `netbox/netbox/settings.py` 新增 `IDEMPOTENCY_KEY_ENABLED`（True）、`IDEMPOTENCY_KEY_RETENTION`（86400 秒）、`IDEMPOTENCY_KEY_LOCK_TIMEOUT`（60 秒）。
  - `netbox/netbox/configuration_example.py` 加入三项配置与注释。
- **Acceptance Criteria Addressed**: AC-8、FR-10
- **Test Requirements**:
  - `rule` TR-1.1: 三项设置存在且默认值正确；`override_settings(IDEMPOTENCY_KEY_ENABLED=False)` 生效。
- **Completion Evidence**:
  - settings.py 第 162-164 行；configuration_example.py 第 162-174 行。
  - `test_feature_disabled`（带键请求无记录）与回收测试通过，确认设置生效。

## Task 2: 新增 IdempotencyKey 存储模型
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 1
- **Description**:
  - `netbox/core/models/idempotency.py`：`IdempotencyKey`（`_netbox_private = True`）含 scope 字段、scope_hash(unique)、fingerprint、status、request_id、response_status/content_type/body/headers、created(db_index)、(status, created) 组合索引；`IdempotencyKeyManager.prune_expired(retention_seconds)`。
  - `core/models/__init__.py` 导出；迁移 `core/migrations/0027_idempotencykey.py` 由 `makemigrations` 生成（FK 行长行已按 ruff 换行）。
- **Acceptance Criteria Addressed**: AC-2、AC-8
- **Test Requirements**:
  - `rule` TR-2.1: 迁移由 makemigrations 生成、migrate 无错；`_netbox_private=True`（不产生 changelog/event/公开 ObjectType）。
  - `rule` TR-2.2: `prune_expired()` 删除过期 complete 记录、保留未过期、返回计数。
- **Completion Evidence**:
  - `manage.py migrate` 成功；`makemigrations --check --dry-run` 返回 No changes detected。
  - `test_prune_expired_records`/`test_housekeeping_job_prunes_records` 通过；创建记录的请求后 ObjectChange 仅反映业务对象（`test_first_request_records_and_retry_replays` 断言 changelog 计数=1）。

## Task 3: 实现幂等键核心逻辑模块
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 2
- **Description**:
  - 新建 `netbox/netbox/api/idempotency.py`：常量、normalize_path、compute_scope_hash、compute_fingerprint（body+显著查询参数，排除 brief/fields/format/omit）、validate_key（400）、`IdempotencyExchange`（engage/run：SET LOCAL lock_timeout→INSERT 占位→执行者执行/finalize/render/存储或 5xx set_rollback；IntegrityError 等待者→重放或 409 冲突；55P03→409 in-progress）、`_build_replay`（HttpResponse + Idempotency-Replayed: true + Location/ETag/Content-Type 回放）、drf-spectacular postprocessing hook。
  - `netbox/netbox/api/exceptions.py` 新增 `IdempotencyKeyConflict`、`IdempotencyKeyInProgress`（均 409）。
- **Acceptance Criteria Addressed**: AC-1-7
- **Test Requirements**:
  - `rule` TR-3.1: fields/omit/brief/format 不影响指纹；background 影响指纹（`test_response_shaping...`、`test_background_param_is_significant...`）。
  - `rule` TR-3.2: 重放响应含 Idempotency-Replayed 且状态码/body/内容类型/Location 一致。
  - `rule` TR-3.3: 锁超时/冲突→409；非法键→400。
- **Completion Evidence**:
  - 模块文件存在并通过 ruff；冲突/非法键/400 重放/background 202 重放测试全部通过（见 Task 8）。
  - 修复两处实现缺陷：HttpResponse 无 `.pop()`（改 `del`）、schema hook 方法名大小写比较（upper）。

## Task 4: 在视图集 dispatch 层接入幂等处理
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 3
- **Description**:
  - `netbox/netbox/api/viewsets/__init__.py` 的 `BaseViewSet` 新增 `dispatch()`：镜像 DRF 3.18 dispatch，在 initial（认证/权限/限流）之后解析 handler，`IdempotencyExchange.engage()` 决定是否介入；执行者在原子块内处理并复用 `exception_to_response()`/`handle_exception()` 翻译；重放/冲突短路；5xx 由 run() 回滚占位。
  - NetBoxModelViewSet.dispatch 既有 try/except 保持为兜底，行为不变；AsyncAPIJob worker 直接调用 action，绕过 dispatch，不受影响。
- **Acceptance Criteria Addressed**: AC-1-7
- **Test Requirements**:
  - `rule` TR-4.1: POST/PUT/PATCH/DELETE 重放正确（DELETE 重放 204、对象保持删除）。
  - `rule` TR-4.2: 重放不新增 ObjectChange、不 flush 事件、background 202 不二次入队。
  - `rule` TR-4.3: 无键/只读/认证失败行为不变。
  - `rule` TR-4.4: 既有 API 测试通过。
- **Completion Evidence**:
  - `netbox.tests.test_api_idempotency` 17 测试通过；`netbox.tests.test_api_background` 19 通过；`dcim.tests.test_api` 1818 测试除 1 个环境性错误（缺编译静态资源 rack_elevation.css，需 collectstatic）外全部通过。

## Task 5: 注册 OpenAPI schema hook
- **Status**: `completed`
- **Priority**: medium
- **Depends On**: Task 3
- **Description**:
  - `SPECTACULAR_SETTINGS['POSTPROCESSING_HOOKS']` 注册 `netbox.api.idempotency.idempotency_key_postprocessing_hook`。
- **Acceptance Criteria Addressed**: AC-9、FR-12
- **Test Requirements**:
  - `rule` TR-5.1: schema 生成成功；post/put/patch/delete 操作含 Idempotency-Key header 参数，get 不含。
- **Completion Evidence**:
  - `manage.py spectacular` 成功；生成 schema.yml 中 `name: Idempotency-Key` 参数出现于所有 unsafe 操作（953 处匹配），GET 操作无此参数。

## Task 6: 系统清理作业回收过期记录
- **Status**: `completed`
- **Priority**: medium
- **Depends On**: Task 2
- **Description**:
  - `core/jobs.py` 的 `SystemHousekeepingJob` 新增 `prune_idempotency_keys()`（按 `IDEMPOTENCY_KEY_RETENTION` 调用 `IdempotencyKey.objects.prune_expired()`，0/None 跳过）并加入 run() 调度。
- **Acceptance Criteria Addressed**: AC-8
- **Test Requirements**:
  - `rule` TR-6.1: prune 方法在小保留期下删除过期记录并记录日志；无保留期 no-op。
- **Completion Evidence**:
  - `test_housekeeping_job_prunes_records` 通过；`core.tests.test_jobs` 全量通过（68 测试批）。

## Task 7: 用户文档
- **Status**: `completed`
- **Priority**: low
- **Depends On**: Task 1, Task 4
- **Description**:
  - `docs/configuration/miscellaneous.md` 新增 IDEMPOTENCY_KEY_ENABLED/RETENTION/LOCK_TIMEOUT 三节。
  - `docs/integrations/rest-api.md` 新增 "Idempotent Writes" 章节及 `Idempotency-Key`/`Idempotency-Replayed` 头说明。
- **Acceptance Criteria Addressed**: AC-9
- **Test Requirements**:
  - `rubric` TR-7.1: 文档完整性；scale 1-5；anchors 1/3/5；threshold >=4。
- **Completion Evidence**:
  - rubric 5：配置参考三项齐备；REST 文档覆盖作用域、语义指纹（body+background 等显著参数）、重放头、409 冲突、并发等待、5xx 释放、4xx 重放、保留期回收。

## Task 8: 测试套件
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 4, Task 5, Task 6
- **Description**:
  - 新建 `netbox/netbox/tests/test_api_idempotency.py`（17 个测试）：首次/重放、changelog 计数、事件 flush 计数、PUT/PATCH/DELETE 重放（含 204 vs 非键 404 对照）、409 冲突、fields/omit 不冲突、background 参数显著性与 202 重放不二次入队、无键穿透、GET 忽略、认证失败不记录、非法键 400、400 校验错误重放、503 释放占位后重试成功、跨用户作用域隔离、开关禁用、prune_expired 与 housekeeping 回收；并发用例为 TransactionTestCase + 4 线程 + Barrier（patch _set_lock_timeout 汇聚）断言单执行者/同结果/3 个重放标记。
- **Acceptance Criteria Addressed**: AC-1-8
- **Test Requirements**:
  - `rule` TR-8.1: 全部测试通过。
  - `rubric` TR-8.2: 覆盖质量；scale 1-5；threshold >=4。
- **Completion Evidence**:
  - `Ran 17 tests ... OK`（含真实线程/真实连接的并发测试）。
  - rubric 5：覆盖正常/重放/冲突/并发（真实 DB 锁）/4xx 重放/5xx 释放/作用域隔离/回收/开关/非法输入全部边界。

## Task 9: 整体自验与 lint
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 8
- **Description**:
  - ruff check 全部改动文件；运行受影响测试模块；spectacular 生成；makemigrations --check。
- **Acceptance Criteria Addressed**: AC-9
- **Test Requirements**:
  - `rule` TR-9.1: ruff 零告警；目标测试通过；schema 生成成功；无缺失迁移。
- **Completion Evidence**:
  - ruff 0.16.6：All checks passed（修复迁移行长行、测试导入排序、lambda-assignment 后）。
  - 测试：netbox.tests.test_api_idempotency 17/17 OK；netbox.tests.test_api_background 19/19 OK；core+netbox API/jobs 68/68 OK；dcim.tests.test_api 1818（仅 1 环境性错误：缺 netbox/static/rack_elevation.css 编译资源）；core/users/extras/netbox.views 1242（仅 4 个 Windows 环境性错误：os.wait4 不存在×2、Windows 路径不支持 URL 文件名×2）。
  - `manage.py spectacular` 成功；`makemigrations --check --dry-run` → No changes detected。
