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
| C3 | 工具调用 | P2 | `core/astrbot.py` | | ✅ 已完成（原生通道 + Planner 兜底） |
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
| G1 | Agent 客户端兼容层 | P1 | `core/astrbot.py` · `api/routes/chat.py` | | ✅ 已完成（0.4.1，0.5.0 扩展） |
| G2 | 原生 system 提示词栈 | P1 | `drivers/protocol/profile.py` | | ✅ 已完成（0.5.2） |
| G3 | 原生客户端工具通道 | P1 | `drivers/protocol/` | | ✅ 已完成（0.5.1） |
| G4 | 中文控制台与项目文档 | P2 | `web/` · `docs/` | | ✅ 已完成（0.5.3） |

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
| 0.5.2 | 原生 system 提示词栈（IDENTITY / SOUL / USER / AGENTS.md） |
| 0.5.3 | 通用 muse.ai 网页反代定位；中文文档体系；全新中文控制台；作者与致谢更新 |

详细记录：

- [docs/PROTOCOL.md](docs/PROTOCOL.md)：协议、线程、系统提示词栈、工具通道
- [docs/CLIENTS.md](docs/CLIENTS.md)：客户端兼容层
- [docs/CLIENT-TEST-REPORT.md](docs/CLIENT-TEST-REPORT.md)：真实客户端与生产验证
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)：分层设计
