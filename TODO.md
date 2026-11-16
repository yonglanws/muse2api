# TODO

本文件记录项目当前的完成情况、预留模块和待办事项，是任务认领的唯一来源。

**认领方式**：开一个标题为 `[Claim] <ID> <任务名>` 的 issue，把 GitHub ID 填到下面的 Owner 列，然后提交 PR，避免重复劳动。开发规范见 [CONTRIBUTING.md](CONTRIBUTING.md)，架构说明见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

优先级：**P0** 必须先完成 · **P1** 核心功能 · **P2** 体验优化 · **P3** 锦上添花

---

## 总览

| ID | 任务 | 优先级 | 位置 | Owner | 状态 |
|---|---|---|---|---|---|
| A1 | 安装依赖，跑通测试与 lint | P0 | `tests/` | | ✅ 已完成 |
| A2 | 浏览器驱动真实账号实测 | P0 | `drivers/browser/` | | ✅ 已完成 |
| A3 | 验证图片 / 视频生成流程 | P0 | `drivers/browser/driver.py` | | ✅ 已完成 |
| A4 | 验证会话续期 | P0 | `upstream/muse.py` | | ✅ 已完成 |
| A5 | 验证 Docker 镜像 | P1 | `Dockerfile` · `Dockerfile.protocol` | | ✅ 已完成 |
| A6 | 启用 GitHub Actions CI | P1 | `.github/workflows/ci.yml` | | ✅ 已完成 |
| B1 | 原生协议驱动（Noise + `chat.stream`） | P1 | `drivers/protocol/` | | ✅ 已完成（0.5.0，0.5.1 增加原生工具通道） |
| B2 | 复用对话线程，降低首字延迟 | P2 | `drivers/protocol/driver.py` | | ✅ 已完成 |
| B3 | 额度查询与按额度调度 | P2 | `MuseDriver.quota` | | 🚧 部分完成（浏览器驱动已接入；协议驱动与调度未接入） |
| C1 | `/v1/responses` | P1 | `api/routes/responses.py` | | ✅ 已完成（官方 SDK 流式 / 非流式验证） |
| C2 | `/v1/images/edits` | P1 | `api/routes/images.py` | | ✅ 已完成（不支持 `mask`） |
| C3 | 工具调用 | P2 | `core/tool_plan.py` · `core/system_prompt.py` | | ✅ 已完成（原生通道 + Planner 兜底） |
| C4 | 流式心跳，避免客户端超时 | P2 | `api/routes/chat.py` | | ✅ 已完成 |
| D1 | 支持更多 Cookie 导入格式 | P1 | `api/routes/admin.py` | | ✅ 已完成 |
| D2 | Cookie 导入浏览器插件 | P2 | `web/extension/` | | ✅ 已完成 |
| D3 | 网页管理控制台 | P2 | `web/dashboard.html` | | ✅ 已完成（0.5.3 全新中文界面） |
| D4 | SQLite / Redis 账号存储 | P3 | `accounts/store.py` | | ⬜ 未开始 |
| E1 | 媒体自动清理 | P2 | `core/media.py` | | ✅ 已完成 |
| E2 | 多 API Key 与限流 | P3 | `api/deps.py` | | 🚧 部分完成（多 Key 已完成；限流未开始） |
| E3 | 可观测性（指标 / 日志） | P3 | `observability/`（新建） | | ⬜ 未开始 |
| F1 | 默认无状态聊天 | P2 | `drivers/browser/driver.py` | | ✅ 已完成 |
| F2 | 显式 `user` 时复用会话 | P2 | `drivers/browser/driver.py` | | ✅ 已完成 |
| F3 | 多用户隔离与并发指引 | P3 | `api/deps.py` · docs | | ✅ 已完成 |
| G1 | 原生客户端工具通道 | P1 | `drivers/protocol/` | | ✅ 已完成（0.5.1） |
| G2 | 中文控制台与项目文档 | P2 | `web/` · `docs/` | | ✅ 已完成（0.5.3） |
| G3 | 纯网页反代重构 | P1 | `api/routes/chat.py` · `core/` | | ✅ 已完成（0.6.0） |
| H1 | 网页对话产物透出（图片/视频/文档 + 来源引用） | P1 | `drivers/protocol/` · `api/routes/chat.py` | | ✅ 已完成（0.11.0） |
| H2 | OpenAI Files API 与 `file_id` 解析 | P1 | `services/file_store.py` · `api/routes/files.py` | | ✅ 已完成（0.11.0） |
| H3 | Responses 检索/删除/input_items/input_tokens/cancel | P1 | `api/routes/responses.py` | | ✅ 已完成（0.11.0） |
| H4 | Chat 参数补齐（max_completion_tokens / parallel_tool_calls / legacy functions / web_search_options） | P1 | `api/routes/chat.py` · `api/schemas.py` | | ✅ 已完成（0.11.0） |
| H5 | Stored Chat Completions（store / list / retrieve / update / delete / messages） | P1 | `services/chat_store.py` · `api/routes/chat.py` | | ✅ 已完成（0.12.0） |
| H6 | tiktoken 精确 token 计数与截断（usage / max_tokens） | P1 | `core/tokens.py` | | ✅ 已完成（0.12.0） |
| H7 | Sora 形状 `/v1/videos`（multipart/JSON、content、remix、分页、状态机） | P1 | `api/routes/videos.py` | | ✅ 已完成（0.12.0） |
| H8 | Responses 对象面补齐（completed_at/service_tier/usage details/instructions 语义/取消持久化） | P1 | `api/routes/responses.py` | | ✅ 已完成（0.12.0） |
| H9 | Files 过期/分页/purpose 校验；Images/Models 字段补齐与不可表达参数 400 | P1 | `api/routes/files.py` · `images.py` · `models.py` | | ✅ 已完成（0.12.0） |
| I1 | 网页同源信封（model.get 接入 / model+timezone+capabilities / 时间标签通道） | P1 | `drivers/protocol/client.py` · `driver.py` · `config.py` | | ✅ 已完成（0.16.0，probe 18） |
| I2 | 原生 persona 通道（onboarding.identity.set vibe / identity 读写管理端点） | P1 | `drivers/protocol/client.py` · `api/routes/admin.py` | | ✅ 已完成（0.16.0，probe 19） |
| I3 | 引用标记归一化（【url_id†L9】→ [n]/删除，流式安全） | P2 | `core/citations.py` · `drivers/protocol/driver.py` | | ✅ 已完成（0.16.0） |
| I4 | VM 推理配置端点（config.get / config.update 透传） | P2 | `api/routes/admin.py` | | ✅ 已完成（0.16.0） |

---

## 待办详情

### B3 额度查询与按额度调度 · P2

- 浏览器驱动已实现 `MuseDriver.quota`：在空闲标签页读取 Settings → Usage（套餐、本周用量、重置时间），缓存 30 分钟，`?refresh=1` 绕过缓存；控制台按账号展示。
- 协议驱动尚未实现额度读取。需要在原生协议上找到等价的 Usage 接口或页面数据来源。
- 调度器可以进一步利用额度快照：优先选择额度充足的账号，额度耗尽的账号提前冷却。
- **完成标准**：协议驱动也能返回额度快照，`/admin/accounts/{id}/quota` 对两种驱动都可用，调度会参考额度。

### D4 SQLite / Redis 账号存储 · P3

- 在 `accounts/store.py` 中实现 `AccountStore` 协议，并通过配置选择后端。
- Redis 后端为多实例部署做准备；届时每账号的并发计数器也需要跨进程共享。
- **完成标准**：账号数据可以在 JSON / SQLite / Redis 之间切换，现有测试全部通过。

### E2 按 Key 限流 · P3

- 多 Key 管理已完成：`POST/GET/PATCH/DELETE /admin/keys`，含用量统计与吊销。
- 增加按 Key 的速率限制（例如每分钟请求数、并发数），并在 `/admin/keys` 中配置。
- **完成标准**：超出限额返回 429，控制台可以查看限额与命中次数。

### E3 可观测性 · P3

- 暴露 Prometheus 指标：请求数、延迟、首 token 延迟、每账号成功 / 失败数、排队时间。
- 为每个请求生成唯一 ID，写入结构化日志，便于排障。
- **完成标准**：`/metrics` 可被 Prometheus 抓取，日志可按请求 ID 串联。

### C5 多步工具链（可选） · P2

- 原生工具通道支持多轮调用，但模型连续拒绝时 Planner 重试仍可能耗尽。
- 可以增加工具调用预算、失败原因分类和更明确的工具结果提示。
- **完成标准**：连续 3 步以上的工具链在真实客户端中稳定完成。

---

## 已完成的重要里程碑

| 版本 | 内容 |
|---|---|
| 0.4.1 | 通用客户端兼容层：Persona bridge、会话隔离、工具 Planner、回复清洗 |
| 0.5.0 | 原生 Noise / `chat.stream` 协议驱动；逐字流、线程隔离、图片与视频 |
| 0.5.1 | 原生客户端工具通道：`register_capabilities` / `client.invoke` → 标准 `tool_calls` |
| 0.5.3 | 通用 muse.ai 网页反代定位；中文文档体系；全新中文控制台 |
| 0.6.0 | 移除平台专用兼容层；纯 OpenAI 协议反代；通用 system 提示词桥接 |
| 0.7.0 | VM 托管规则（IDENTITY/SOUL/AGENTS）+ 原生工具多调用 + 记忆隔离 |
| 0.8.0 | 单账号并发池：每账号多 Noise 连接、线程粘连接、12 并发 |
| 0.9.0 | 中止安全：`chat.cancel`、中断轮次丢弃、JSON 模式、stop、n>1、CORS |
| 0.10.0 | 错误/头/多选/Responses 状态与函数调用；file_ref 任意附件；严格 muse 模型名 |
| 0.11.0 | 网页对话产物（图片/视频/文档）与来源引用透出；Files API；Responses 检索面；Chat 参数补齐 |
| 0.12.0 | Stored Chat Completions；tiktoken 用量/截断；Sora 形状 `/v1/videos`；Responses 对象面与 instructions 语义；Files 过期/分页 |
| 0.12.1 | 多 Key 数据隔离；Responses SSE `event:`/retrieve stream；legacy function_call；function_call_output 附件；file_url；JSON 模式媒体；过期文件字节清理；删除墓碑 |
| 0.12.2 | legacy function_call 计入 completion usage；过期文件按 id 查询也回收字节；同时间戳 Files 分页不丢条目 |
| 0.13.0 | OpenAI Uploads 分片上传（64 MiB/片，完成后生成 Files 对象）；图片 output_format/output_compression 本地转码 |
| 0.13.1 | 生产镜像内置 `[matting]` extra（rembg + pymatting），预下载 BiRefNet-lite；`background=transparent` 真正返回 RGBA PNG |
| 0.14.0 | OpenAI Conversations API（CRUD + items 分页）；Responses `conversation` 自动前置历史并在完成后追加本轮 |
| 0.15.0 | `/v1/responses/compact`（本地摘要 compaction item，可回传）；视频 `thumbnail`/`spritesheet`（本地 ffmpeg + 缓存） |

详细记录：

- [docs/PROTOCOL.md](docs/PROTOCOL.md)：协议、线程、系统提示词栈、工具通道
- [docs/CLIENTS.md](docs/CLIENTS.md)：客户端兼容层
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)：分层设计
