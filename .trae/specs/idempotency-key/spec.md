# NetBox REST API 幂等键（Idempotency-Key）支持 - 产品需求文档

## Overview
- **Summary**：为 NetBox REST API 的受支持写请求（POST、PUT、PATCH、DELETE）增加 `Idempotency-Key` 请求头支持。在同一已认证主体、HTTP 方法、规范化路径的作用域内，幂等键唯一标识一次请求；首次请求完成后保存其语义指纹与最终响应，后续相同请求直接重放原结果而不再执行写入；键相同但请求语义不同时返回 409 冲突；并发同键请求只有一个执行者。
- **Purpose**：网络自动化控制器（以及其他 API 客户端）在代理超时、连接中断后安全重试写请求，避免重复创建对象、重复记录 changelog、重复触发 event rule 或重复排队 background bulk job。
- **Target Users**：通过 REST API 对 NetBox 执行自动化写入的网络控制器/集成开发者；NetBox 运维管理员（配置保留期）。

## Goals
- 受支持的写请求接受 `Idempotency-Key` 请求头，作用域为（已认证主体、HTTP 方法、规范化路径、键）。
- 首次请求完成后，保存语义指纹（请求体 + 影响写入语义的查询参数）与最终 HTTP 状态码、响应体、必要响应头。
- 同作用域、同键、同语义的后续请求：不再次写库、不重复记录 changelog、不触发 event rule、不重复排队 background bulk job，直接重放原结果，并通过响应头明确标示这是重放。
- 同作用域复用键但请求体或影响写入语义的查询参数不同：返回 409 冲突且不执行请求。
- 并发到达的同键请求：恰好一个成为执行者，其余请求在首次执行完成后取得同一结果。
- 未提供键、只读请求、现有认证失败流程的行为与今天完全一致。
- 超过保留期的记录可按配置回收。
- 提交前发生的服务器异常（5xx/未捕获异常）不得留下会永久阻塞后续重试的占位状态。

## Non-Goals
- 不改变无 `Idempotency-Key` 请求的任何行为。
- 不为 GraphQL API、UI（HTML 表单）请求提供该机制。
- 不基于 NetBox 标准视图集的自定义 `APIView` 端点（如 `TokenProvisionView` 这类非认证/特殊端点）默认启用；但机制以可复用形式提供，插件/自定义视图可自行接入。
- 不提供幂等记录的 UI 浏览/管理界面（该模型为内部基础设施，`_netbox_private`）。
- 不引入新的第三方依赖。

## Background & Context
- DRF 视图层：所有模型写端点继承 `netbox.api.viewsets.BaseViewSet`（经由 `NetBoxModelViewSet`），插件视图集同样继承它。DRF `APIView.dispatch()` 负责 `initialize_request → initial（认证/权限/限流）→ handler → handle_exception → finalize_response`；认证主体 `request.user` 在 `initial()` 之后可用，因此幂等逻辑必须挂在 DRF dispatch 层（Django 中间件层在视图执行前无法获知 token 认证主体）。
- `NetBoxModelViewSet.dispatch()` 已将 `ProtectedError`/`RestrictedError`（409）与 `AbortRequest`（400）翻译为 Response；同类逻辑已抽取为 `exception_to_response()`，供绕过 dispatch 的 `AsyncAPIJob` worker 使用，本功能复用同一翻译入口。
- 写副作用：changelog 与 event 由模型信号在事务内入队（`extras.events.enqueue_event`，线程局部队列），`netbox.context_managers.event_tracking` 请求处理器在视图成功完成后统一 flush；background bulk job 由 `BackgroundOperationMixin._enqueue_bulk_job()` 入队并返回 202。重放路径不执行 handler，即不产生任何上述副作用。
- 并发与崩溃安全：PostgreSQL 唯一约束 + 事务行锁可实现"插入占位行”互斥：未提交的 INSERT 会阻塞并发同键 INSERT；执行者提交后并发者收到唯一冲突 → 读取已完成记录重放；执行者回滚（含 5xx 释放、进程崩溃导致连接断开）后并发者的 INSERT 成功，自动接管执行。
- 回收：`core.jobs.SystemHousekeepingJob`（`@system_job(interval=INTERVAL_DAILY)`）已按配置保留期清理 `ObjectChange` 与 `Job`，幂等记录回收接入同一作业。
- 内部模型约定：`_netbox_private = True` 的模型不创建公开 ObjectType、不产生 changelog/event（`core.signals` 跳过），亦不受 `test_model_test_coverage` 的公开模型覆盖约束。
- 配置约定：`netbox/settings.py` 以 `getattr(configuration, 'NAME', default)` 读取静态配置；文档见 `docs/configuration/miscellaneous.md`；OpenAPI 由 drf-spectacular 生成，`SPECTACULAR_SETTINGS['POSTPROCESSING_HOOKS']` 目前为空列表。

## Functional Requirements
- **FR-1**：受支持端点（继承 NetBox 视图集基类的端点）上，已认证的 POST/PUT/PATCH/DELETE 请求携带 `Idempotency-Key` 请求头时启用幂等处理；作用域为（用户 PK、HTTP 方法、去掉尾部斜杠的规范化路径、键值）。
- **FR-2**：首次请求（执行者）在执行写入前插入占位记录（状态 in-progress，含语义指纹）；请求完成后保存最终响应：HTTP 状态码、渲染后的响应体、响应内容类型、白名单响应头（`Location`、`ETag`），状态置为 complete。
- **FR-3**：语义指纹 = 请求体原始字节 + 规范化的查询参数（排除仅影响响应呈现的参数 `fields`、`omit`、`brief`、`format`；其余参数如 `background` 均参与指纹）。
- **FR-4**：同作用域、同键、同指纹的后续请求：不执行 handler，直接重放保存的响应（相同状态码、响应体、内容类型、Location/ETag），并附加响应头 `Idempotency-Replayed: true`。
- **FR-5**：同作用域、同键但指纹不同（请求体不同，或影响写入语义的查询参数不同）：返回 409 冲突响应，且不执行任何写入。
- **FR-6**：并发同键请求：通过数据库唯一约束与行锁保证恰好一个执行者；等待者阻塞至首次执行结束，随后重放同一结果；等待设置有界锁等待超时，超时返回 409 提示稍后重试。
- **FR-7**：执行者返回 5xx 或抛出未处理异常时，占位记录随事务回滚而释放（不保存），客户端可用同键重试并重新执行；4xx 终端响应（含 400/403/404/409/412）作为已完成结果保存并重放。
- **FR-8**：认证/权限/限流失败（401/429 等发生在幂等检查之前的拒绝）不创建任何记录，行为与现状一致；未携带键的请求、GET/HEAD/OPTIONS 请求完全不受影响。
- **FR-9**：键值校验：空值/过长（>255 字符）/含控制字符的键返回 400。
- **FR-10**：配置项：`IDEMPOTENCY_KEY_ENABLED`（默认 True）、`IDEMPOTENCY_KEY_RETENTION`（秒，默认 86400；0/None 表示永久保留）、`IDEMPOTENCY_KEY_LOCK_TIMEOUT`（秒，默认 60）。
- **FR-11**：超过保留期的 complete 记录由每日系统清理作业（SystemHousekeepingJob）自动删除；模型管理器同时提供可直接调用的删除方法。
- **FR-12**：OpenAPI schema 中为不安全操作标注 `Idempotency-Key` 头参数；配置示例与文档（配置参考、REST API 指南）说明该功能。

## Non-Functional Requirements
- **NFR-1**：无键请求不得有可观测的行为变化或有意义的性能回退（幂等逻辑不触发额外数据库访问）。
- **NFR-2**：实现遵循现有架构模式（DRF dispatch、core 模型、settings getattr 配置、ruff 单引号/行宽 120），不引入新依赖。
- **NFR-3**：幂等记录本身的写入不产生 changelog/event/指标以外的副作用，不干扰现有事件 flush、事务、on_commit 语义。
- **NFR-4**：测试覆盖重放、冲突、并发、失败释放、作用域隔离、保留期回收，使用 Django 测试框架（TestCase + 并发场景用 TransactionTestCase）。

## Constraints
- **Technical**：Python 3.12+/Django 6.x/DRF 3.18/PostgreSQL；迁移必须由 `manage.py makemigrations` 生成，不得手写；不使用 `ruff format`；代码风格单引号、行宽 120。
- **Business**：不破坏既有 API 行为契约；插件视图集自动获得能力且无需改动。
- **Dependencies**：仅使用 Django/DRF/PostgreSQL 既有能力；`AsyncAPIJob` worker 路径绕过 dispatch，不得受影响。

## Assumptions
- “受支持的写请求”指经由 NetBox 标准视图集（`BaseViewSet` 体系，含全部模型 CRUD、批量写、background bulk 写与 @action 写端点）处理的请求。
- 4xx 终端响应属于“第一次请求已完成”，保存并重放是安全的（4xx 不产生已提交写入）；5xx 属于“提交前/服务端异常”，释放占位。
- 重放标示采用响应头 `Idempotency-Replayed: true`。
- 路径规范化取 `request.path` 去除尾部斜杠；部署内 BASE_PATH 一致，不影响作用域正确性。
- 进程硬崩溃由数据库连接断开/事务回滚兜底，占位行不会残留；仅做 complete 记录的保留期回收。

## Acceptance Criteria

### AC-1: 幂等键作用域与启用条件
- **Type**: `rule`
- **Given**：已认证客户端对受支持写端点（如 POST /api/dcim/regions/）发起请求
- **When**：携带 `Idempotency-Key: abc123`
- **Then**：系统在（用户、方法、规范化路径、键）作用域内创建/使用幂等记录；未携带键的写请求、GET/HEAD/OPTIONS 请求不产生任何幂等记录
- **Pass Condition**：带键首次写请求后存在一条作用域正确的记录；无键写请求与只读请求后无记录
- **Evidence**：`netbox/tests/test_api_idempotency.py` 中启用条件与无键穿透测试；模型记录断言

### AC-2: 首次完成后保存指纹与最终响应
- **Type**: `rule`
- **Given**：执行者请求成功或返回 4xx 终端响应
- **When**：请求处理完成
- **Then**：记录保存语义指纹、HTTP 状态码、渲染后响应体、内容类型与 Location/ETag 头，状态为 complete
- **Pass Condition**：记录字段与实际响应一致（状态码、body 文本、内容类型、白名单头）
- **Evidence**：测试中对记录字段与响应一致性的断言

### AC-3: 同键同语义重放且无重复副作用
- **Type**: `rule`
- **Given**：同用户、同方法、同路径、同键、同请求体与查询参数的第二次请求
- **When**：首次请求已完成
- **Then**：返回与首次相同的状态码/响应体/必要响应头，且响应含 `Idempotency-Replayed: true`；不发生第二次写库（对象数量不变）、不新增 ObjectChange、不 flush 新事件、不重复入队 background job
- **Pass Condition**：重复 POST 只创建一个对象；ObjectChange 计数为 1；重放响应带标记头且与首次响应体一致；background 202 重放不二次入队
- **Evidence**：重放测试（POST/PUT/PATCH/DELETE、bulk background）中断言对象数、ObjectChange 数、job 数与重放头

### AC-4: 同键不同语义返回冲突且不执行
- **Type**: `rule`
- **Given**：同作用域同键的第二次请求请求体不同，或影响写入语义的查询参数不同（如 `background`）
- **When**：首次请求已完成
- **Then**：第二次请求返回 409，且不执行写入；仅影响响应呈现的参数（`fields`/`omit`/`brief`/`format`）不同不构成冲突，正常重放
- **Pass Condition**：不同 body → 409 且无新对象；不同 `fields` → 200/201 重放
- **Evidence**：冲突与响应形参测试

### AC-5: 并发同键请求单执行者
- **Type**: `rule`
- **Given**：多个线程/连接同时发起同键同体写请求（使用屏障确保真正并发）
- **When**：请求并发到达
- **Then**：恰好一个请求成为执行者，其余请求在首次执行完成后重放同一结果；数据库中只产生一次写入效果
- **Pass Condition**：N 个并发 POST 后仅创建 1 个对象；全部响应状态码与响应体一致；恰有一个响应无重放标记
- **Evidence**：基于 TransactionTestCase + 线程的并发测试

### AC-6: 失败与认证流程不被破坏
- **Type**: `rule`
- **Given**：(a) 执行者处理中返回 5xx/抛出未捕获异常；(b) 未认证/令牌无效请求携带键；(c) 键格式非法
- **When**：相应请求发出
- **Then**：(a) 占位记录释放，同键重试可重新执行并成功；(b) 返回与现状一致的 401，且不创建记录、后续合法请求不受阻；(c) 返回 400
- **Pass Condition**：503（无 RQ worker）后同键在 worker 可用时成功；无效令牌 401 且无记录；空/超长键 400
- **Evidence**：失败释放、认证失败穿透、非法键测试

### AC-7: 作用域隔离
- **Type**: `rule`
- **Given**：两个不同已认证用户（或同一用户对不同路径/方法）使用相同键值
- **When**：分别发起请求
- **Then**：互不影响，各自成为执行者并独立完成
- **Pass Condition**：不同用户同键各创建一个对象、均无重放标记；不同路径同键不冲突
- **Evidence**：作用域隔离测试

### AC-8: 保留期回收与功能开关
- **Type**: `rule`
- **Given**：存在 complete 幂等记录
- **When**：保留期已过且系统清理作业运行（或管理器方法被调用）；或将 `IDEMPOTENCY_KEY_ENABLED` 置为 False
- **Then**：过期记录被删除、未过期记录保留；功能关闭时带键请求与无键行为完全一致（不创建记录）
- **Pass Condition**：prune 调用后过期记录删除、未过期保留；关闭开关后带键写请求不产生记录
- **Evidence**：回收测试与开关测试

### AC-9: 实现质量与项目一致性
- **Type**: `rubric`
- **Dimension**：代码与 NetBox 既有架构/风格的一致性、可维护性、文档与 schema 完整性
- **Scale**: 1-5
- **Anchors**: 1 = 绕过既有模式另起炉灶或破坏现有流程；3 = 功能可用但风格/接入点生硬、缺文档或 schema；5 = 复用 dispatch/异常翻译/系统作业等既有机制，配置、文档、OpenAPI、测试齐备，ruff 通过
- **Pass Threshold**: >= 4
- **Evidence**: 代码审查、`ruff check` 结果、新增测试运行结果、文档与 schema hook

## Open Questions
- 无阻塞性开放问题（重放标记头名称、指纹参数白名单、默认保留期等已按 Assumptions 确定，可在评审时调整）。
