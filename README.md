# muse2api

> muse.ai 是住在网页里的 Agent。muse2api 在它外面加了一层标准接口，让任何 OpenAI 客户端都能把它当作模型使用。

muse2api 是一个自托管的**网页反代网关**：对外提供 `/v1/chat/completions`、`/v1/images/generations`、`/v1/videos` 这些熟悉的 OpenAI 接口，对内通过原生 Noise 协议连接 muse.ai 的网页 Agent。它不模拟网页点击，也不把提示词简单拼在聊天框里，而是直接使用 muse.ai 自己的会话、线程和客户端工具通道。

这一版是 **0.8.0**。

## 先说结论

- **一个地址就能接入**：把 `base_url` 指向 `http://<host>:18610/v1`，OpenAI SDK、LobeChat、Cherry Studio 或自研 Agent 都能用。
- **不需要 Chromium**：推荐 `MUSE2API_DRIVER=protocol`，直接用 Noise + `chat.stream` 通信，镜像约 194 MB。
- **单账号真高并发**：逆向实测上游按 VM 并行执行轮次（单连接 12 条并发流零错误），网关用连接池 + 预热把这份并发能力完整交给调用方，默认单账号 12 并发。
- **系统提示词生效**：`system` / `developer` 消息会被整理成独立指令块，再和当前用户消息一起发送。
- **工具调用真的执行**：OpenAI `tools` 会被注册成 muse.ai 的原生客户端命令，Agent 在自己的工具循环里调用，再还原成标准 `tool_calls`。
- **多账号、多会话**：账号池负责调度、冷却、故障转移；会话按 API Key 隔离，热线程复用降低首字延迟。
- **图片和视频**：文生图、参考图、异步任务、文生视频、透明背景抠图都在同一个网关里。

它**不是**某个聊天平台的插件，也**不是**一个只会拼接提示词的外壳。所有客户端都走同一套 OpenAI 协议，兼容层只在请求带有 `system`、`tools` 或会话标识时自动介入。

## 三分钟跑起来

### 1. 启动

```bash
cp .env.example .env
docker compose up -d --build
```

服务默认监听 `127.0.0.1:18610`。首次启动如果没有设置 `MUSE2API_API_KEY`，会自动生成一个密钥并保存到 `data/api_key`。

本地开发可以不用 Docker：

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e '.[dev]'
python -m muse2api
```

默认的 `mock` 驱动不需要账号、不访问网络，适合先跑通接口和前端。

### 2. 放入一个 muse.ai 账号

从已登录 muse.ai 的浏览器导出 Cookie，至少需要 `hatch_sess` 和 `hatch_native_auth_device`：

```bash
curl http://localhost:18610/admin/accounts \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"label":"main","cookies":{"hatch_sess":"...","hatch_native_auth_device":"..."}}'
```

也可以打开 `http://localhost:18610/dashboard`，在「账号池」页下载 Chrome 插件，一键读取 httpOnly Cookie 并导入。

### 3. 把客户端指过来

| 字段 | 值 |
|---|---|
| API Base | `http://<网关地址>:18610/v1` |
| API Key | `MUSE2API_API_KEY`，或控制台创建的 `m2a-...` 密钥 |
| 模型 | `muse-chat`，别名 `gpt-4o` / `dall-e-3` |
| 超时 | 建议 ≥ 240 秒（muse.ai 冷启动较慢） |

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:18610/v1", api_key="<API_KEY>")
resp = client.chat.completions.create(
    model="muse-chat",
    messages=[{"role": "user", "content": "写一首关于秋天的短诗"}],
)
print(resp.choices[0].message.content)
```

## 请求怎么走

```text
OpenAI 客户端
     │  HTTP + Bearer Key
     ▼
┌──────────────┐   账号池 / 故障转移 / 会话隔离 / 请求日志
│   muse2api   │───────────────────────────────────────────┐
└──────────────┘                                           │
     │  Noise XX + protobuf + chat.stream                   │
     ▼                                                      ▼
muse.ai 网页 Agent  ──  系统指令 · 线程 · 客户端工具  ──  图片 / 视频
```

一次聊天请求会经历这些步骤：

1. 路由解析模型别名，把多轮 `messages` 合并成一条提示词；
2. Gateway 从账号池租一个账号，驱动建立或复用 muse.ai 线程；
3. 上游的 `delta.text_append` 直接变成 OpenAI `delta.content`；
4. 出错时按类型更新账号状态，在尚未向客户端发送内容前自动换号重试；
5. 流式请求先拿到第一个 chunk 再返回 200，避免“中途断流”。

## 系统提示词

muse.ai 的 `chat.stream` 没有独立的 `system` 角色。0.7.0 起网关用两层机制让 system 语义真正生效：

- **VM 托管规则**：muse.ai 的 Agent 每次会话开始都会读取账号 VM 里的 `IDENTITY.md` / `SOUL.md` / `AGENTS.md`，服务端还会逐轮把它们重新注入上下文——这是 muse.ai 上最接近 system role 的通道。网关在这三个文件末尾维护一段带标记的托管规则（API 会话跟随 system 指令、禁用账号记忆工具、优先使用已注册客户端命令）。原文件先备份到 `data/harness_backup/`，`POST /admin/harness/{id}/restore` 一键还原；
- **会话内指令块**：`system` / `developer` 消息被收集成一个指令块，放在提示词最前面；当前用户消息单独留在最后；同一 `user` 的后续请求只发送新的用户轮；`system` 内容变化时指令指纹变化，驱动会开一个新线程。

实测效果（真实 muse.ai 全链路）：带 system 的会话里，模型自称 system 定义的姓名、精确跟随输出格式（如"恰好两个短句"）、不泄露账号主人的记忆。对协议与注入边界的完整逆向记录见 [docs/PROTOCOL.md](docs/PROTOCOL.md)。

过长的系统指令会按 `MUSE2API_MAX_PROMPT_CHARS` 截断，优先保留末尾的对话与任务说明。

## 工具调用

muse.ai 网页端有一套客户端工具协议。网关把它接成了标准 OpenAI 工具调用：

1. 请求带 `tools` 时，调用 `client.register_capabilities` 注册 `ui.navigate.<工具名>` 命令；
2. Muse 在自己的工具循环里调用命令，网关收到 `client.invoke` 事件（一轮多条调用也会全部捕获）；
3. 网关立即应答，避免命令超时，并把调用重编码为标准 `tool_calls`；
4. 客户端把工具结果发回来，网关续接同一线程继续对话；
5. Planner 作为兜底输出契约，Muse 走哪条路径都能产出 `tool_calls`。

```json
{
  "id": "call_...",
  "type": "function",
  "function": {
    "name": "get_group_member_info",
    "arguments": "{\"group_id\":\"123456\",\"user_id\":\"789\"}"
  }
}
```

`finish_reason` 为 `tool_calls`，流式请求返回标准 `delta.tool_calls`。

| 配置 | 说明 |
|---|---|
| `MUSE2API_TOOL_CHANNEL=auto` | 默认，优先原生通道，Planner 兜底 |
| `native` | 只用原生通道 |
| `planner` | 只用扁平 JSON Planner |

## 接口

所有 `/v1/*` 使用 `Authorization: Bearer <API_KEY>`；`/admin/*` 使用 `MUSE2API_ADMIN_KEY`（未设置时回退到 API Key）。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` · `/readyz` | 存活 / 就绪检查 |
| GET | `/v1/models` | 模型列表与别名 |
| POST | `/v1/chat/completions` | 聊天，流式、图片输入、工具调用 |
| POST | `/v1/responses` | OpenAI Responses API |
| POST | `/v1/images/generations` · GET `/{id}` | 文生图；`"async": true` 返回任务 |
| POST | `/v1/images/edits` | multipart 参考图 + 提示词 |
| POST | `/v1/videos` · GET `/{id}` | 创建 / 查询视频任务 |
| GET | `/v1/media/{name}` | 下载生成的媒体 |
| GET/POST/PATCH/DELETE | `/admin/accounts[/{id}]` | 账号增删改查 |
| POST | `/admin/accounts/import` | 批量导入，逐条返回结果 |
| POST | `/admin/accounts/{id}/renew` · `/reset` | 续期 / 重置 |
| GET | `/admin/accounts/{id}/quota[?refresh=1]` | 额度快照 |
| GET/POST/PATCH/DELETE | `/admin/keys[/{id}]` | API Key 管理 |
| GET | `/admin/requests` · `/admin/stats` | 请求日志与聚合 |
| GET | `/admin/status` · `/admin/tasks` | 服务状态 / 任务列表 |
| GET | `/admin/extension.zip` | Cookie 导入插件 |
| GET | `/dashboard` | 管理控制台 |

## 图片与视频

```bash
# 文生图
curl http://localhost:18610/v1/images/generations \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"prompt":"雨夜的赛博朋克街道","size":"16:9"}'

# 参考图
curl http://localhost:18610/v1/images/generations \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"prompt":"模特穿着这件卫衣，棚拍","image":"data:image/jpeg;base64,..."}'

# 异步图片
curl http://localhost:18610/v1/images/generations \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"prompt":"一只红狐狸","async":true}'

# 文生视频
curl http://localhost:18610/v1/videos \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"prompt":"枫叶在微风中飘落","duration":5,"size":"16:9"}'
```

`background: "transparent"` 会在本地用 BiRefNet 抠图并返回 RGBA PNG，需要 `pip install -e '.[matting]'`，模型首次使用时会下载到 `~/.u2net`。

## 配置

配置来自环境变量（前缀 `MUSE2API_`）或 `.env`，完整列表见 [.env.example](.env.example)。

| 变量 | 默认值 | 作用 |
|---|---|---|
| `MUSE2API_DRIVER` | `mock` | `mock` / `protocol` / `browser` / `http` |
| `MUSE2API_HOST` · `MUSE2API_PORT` | `127.0.0.1` · `18610` | 监听地址与端口 |
| `MUSE2API_API_KEY` | 自动生成 | `/v1/*` 密钥 |
| `MUSE2API_ADMIN_KEY` | 同 API Key | `/admin/*` 密钥 |
| `MUSE2API_UPSTREAM_PROXY` | 空 | 协议连接、续期、VM 唤醒的代理 |
| `MUSE2API_BROWSER_PROXY` | 空 | 浏览器驱动专用代理 |
| `MUSE2API_POOL_STRATEGY` | `affinity` | `lru` / `round_robin` / `affinity` |
| `MUSE2API_ACCOUNT_MAX_CONCURRENCY` | `12` | 每账号并行请求数（上游实测 12 并发无压力） |
| `MUSE2API_PROTOCOL_CONNECTIONS` | `2` | 每账号并行 Noise 连接数，并发轮次分摊其上 |
| `MUSE2API_WARM_THREADS` | `2` | 浏览器驱动预热线程数 |
| `MUSE2API_MAX_FAILOVER` | `2` | 换账号重试次数 |
| `MUSE2API_ACCOUNT_COOLDOWN` | `120` | 错误后的冷却秒数 |
| `MUSE2API_TOOL_CHANNEL` | `auto` | `auto` / `native` / `planner` |
| `MUSE2API_MAX_PROMPT_CHARS` | `18000` | 系统指令与历史的最大长度 |
| `MUSE2API_TOOL_RETRIES` | `2` | Planner 被拒绝时的重试次数 |
| `MUSE2API_NATIVE_HARNESS` | `true` | 在账号 VM 的 IDENTITY/SOUL/AGENTS.md 维护托管规则，备份可还原 |
| `MUSE2API_EMULATE_TOOLS` | `true` | 是否启用工具调用 |
| `MUSE2API_KEEPALIVE_ENABLED` | `true` | 后台续期与 VM 唤醒 |
| `MUSE2API_KEEPALIVE_INTERVAL` | `600` | 保活间隔（秒） |
| `MUSE2API_CHAT_TIMEOUT` | `300` | 聊天总超时（秒） |
| `MUSE2API_FIRST_TOKEN_TIMEOUT` | `75` | 首 token 超时（秒） |
| `MUSE2API_STREAM_HEARTBEAT_INTERVAL` | `0` | SSE 心跳间隔，`0` 关闭 |
| `MUSE2API_MEDIA_RETENTION_DAYS` | `14` | 媒体保留天数 |
| `MUSE2API_REQUEST_LOG_RETENTION_DAYS` | `14` | 请求日志保留天数 |

推荐的生产配置：

```bash
MUSE2API_DRIVER=protocol
MUSE2API_UPSTREAM_PROXY=http://<proxy>:7893
MUSE2API_ACCOUNT_MAX_CONCURRENCY=12
MUSE2API_KEEPALIVE_ENABLED=true
MUSE2API_TOOL_CHANNEL=auto
```

## 部署

纯协议镜像不带 Chromium，约 194 MB：

```bash
docker build --network host \
  --build-arg HTTPS_PROXY=http://<proxy>:7893 \
  -f Dockerfile.protocol \
  -t muse2api:latest .

docker run -d --name muse2api \
  --restart unless-stopped \
  -p 18610:18610 \
  -v /path/to/data:/app/data \
  -e MUSE2API_DRIVER=protocol \
  -e MUSE2API_UPSTREAM_PROXY=http://<proxy>:7893 \
  muse2api:latest
```

放在 Nginx / Caddy / Cloudflare 后面时：

- 关闭响应缓冲，确保 SSE 逐字输出；
- 读取超时 ≥ 300 秒，视频任务建议 600 秒；
- 保留 `Authorization` 请求头；
- 如果反向代理有约 100 秒的限制，图片生成请使用 `async` 任务。

数据都在 `data/`：`accounts.json`、`keys.json`、`requests.db`、`media/`。升级前备份这个目录即可。

## 控制台

打开 `http://<host>:18610/dashboard`，输入管理密钥。总览看流量和账号健康度，账号池管导入、续期、额度与启停，API 密钥按人分发，请求日志可以按密钥、账号、状态码和路径过滤。界面是单文件、无外部依赖，支持明暗主题和移动端。

![muse2api 控制台](docs/images/dashboard-overview.png)

## 它不擅长什么

1. muse.ai 是 Agent，不是可完全控制的 LLM；它仍可能拒绝某些请求，或输出与普通 LLM 不同的结果。
2. 工具调用优先走原生通道，Planner 是兜底；连续拒绝、重试耗尽后可能返回普通文本而不是 `tool_calls`。
3. API 会话里的账号级记忆已被托管规则禁用（实测有效），但 muse.ai 上游的后台整理流程仍可能往账号记忆里写笔记；网关不删除、不改写账号的 MEMORY.md。
4. 没有 `system` 消息、只用普通用户文本描述身份时，直接问"你是谁"仍可能自称 Muse——这与 OpenAI 语义一致（user 弱于 system）。
5. 历史图片只保留占位符，跨线程依赖完整图片细节时可能丢失。
6. 单账号并发已按上游实测放开（默认 12，逆向验证单连接 12 条并行流无错误）；要更高并发仍建议增加账号——一个 muse.ai 账号运行在一台 VM 上，并发高时各轮次共享同一台 VM 的算力与配额。

## 来路

这个项目的协议层、账号池和管理台不是凭空长出来的。它站在两个开源项目的工作之上：

- **[www222fff/muse2api](https://github.com/www222fff/muse2api)**：本项目的起点。OpenAI 协议层、账号池、驱动分层、管理后台和 Cookie 导入插件等基础实现来自这里。
- **[lichao199208/muse2api](https://github.com/lichao199208/muse2api)**：纯协议反代、号池调度与任务接口的另一条实现路线，本项目的协议层设计从中获得参考。

也感谢 [muse.ai](https://muse.ai) 提供了这样一个能力完整的网页 Agent。

更细的文档：

- [docs/PROTOCOL.md](docs/PROTOCOL.md)：Noise 握手、线程、系统提示词、工具通道
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)：分层与请求生命周期
- [docs/CLIENTS.md](docs/CLIENTS.md)：OpenAI 客户端接入

## 许可

[MIT](LICENSE)

本项目仅用于学习、技术研究与自托管实践。请遵守 muse.ai 的服务条款和当地法律，不要用于商业转售、批量注册或其他滥用行为。切勿提交或公开账号 Cookie。
