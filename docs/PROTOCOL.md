# 纯协议驱动（Noise / `chat.stream`）

0.5.0 新增 `MUSE2API_DRIVER=protocol`。它不再启动 Chromium，而是直接连接 muse.ai 网页端使用的 Hatch 通道：

```
GET  /api/session?vm_state=live
POST /api/hatch/token
WSS  wss://hatch.metaaivm.com/v1/noise      Noise_XX_25519_AESGCM_SHA256
     └─ protobuf NoiseTransportFrame → ServiceFrame → ApplicationRequest
POST /chat/stream                           提交一轮用户消息
POST /chat/subscribe                        订阅一个 main/thread 的 NDJSON 事件流
POST /fs/read                               取回生成的图片/视频
```

实现位于 `src/muse2api/drivers/protocol/`：

| 文件 | 作用 |
|---|---|
| `noise.py` | Noise XX 握手、AES-GCM 帧、分片重组 |
| `hatch_pb2.py` | 上游 protobuf 定义 |
| `client.py` | HTTP 引导、WebSocket、RPC、订阅、`fs.read` |
| `driver.py` | 账号连接复用、线程映射、逐字流、图片/视频 |
| `media.py` | 成品资源提取与 MIME 判断 |

## 1. 开启

```bash
MUSE2API_DRIVER=protocol
MUSE2API_UPSTREAM_PROXY=http://<proxy>:7893   # Python HTTP 和 WebSocket 都走它
MUSE2API_ACCOUNT_MAX_CONCURRENCY=4
MUSE2API_PROTOCOL_MAX_THREADS=64
MUSE2API_PROTOCOL_THREAD_TTL=21600
```

依赖：`cryptography`、`protobuf`、`python-socks[asyncio]`、`websockets>=15`。`Dockerfile.protocol` 是不带 Chromium 的 194 MB 镜像。

## 2. 为什么它比浏览器驱动更接近 API

- **逐字流**：`delta.text_append` 直接映射为 OpenAI `delta.content`，不是轮询 DOM 后用稳定文本切片。
- **真正的线程**：每个会话一个 `session_id`，线程订阅只接收该线程的事件；不同用户的上下文在协议层就分开。
- **线程续接**：第二轮只发送新的用户消息，不需要重发全量历史；实测第二轮首字 3–10 秒。
- **图片/视频**：`delta.presentation` / `delta.tool_done` 给出 sandbox 资源路径，`fs.read` 取回原始文件。
- **无浏览器**：不需要 Chromium、tab 池或 profile；内存和启动成本大幅下降。

## 3. 线程模型

1. 网关为一个新的 conversation key 生成随机 UUID 作为 `session_id`，随 `chat.stream` 发送。
2. 上游 ack 返回 `is_thread: true`，并把这个会话当作线程。
3. 网关立刻用同一个 `session_id` 调 `chat.subscribe`，从 `seq 0` 重放本轮事件。
4. 后续轮次复用同一个 `session_id` 和订阅；只发送 `followup_text()` 计算出的新用户消息。
5. 系统提示词指纹变化、历史前缀不匹配、或重试要求 `force_new_thread` 时，换一个新的 UUID，旧线程不再使用。
6. 每个账号最多保留 `MUSE2API_PROTOCOL_MAX_THREADS` 个线程；超过后按最久未用淘汰，订阅同时关闭。淘汰后若再次使用同一个 conversation key，会新建线程并用完整历史提示词，不会串到别人的线程。
7. 匿名请求（没有 `user`）每次都用一次性线程，不落表。

> 上游的账号级记忆仍是共享的。网关通过 `[[SESSION ISOLATION]]` 前导和线程隔离降低串扰，但不能从协议层删除账号记忆。

### 3.1 单账号并发与连接池（0.8.0）

对上游并发能力的逆向实测（真实 muse.ai，单账号、单条 Noise 连接）：

| 场景 | 结果 |
|---|---|
| 8 条并行 `chat.stream`（各自独立 session） | 全部成功，墙钟 12.0s，零错误，无事件串扰 |
| 12 条并行 `chat.stream` | 全部成功，墙钟 14.6s，零错误 |
| 15 条背靠背顺序请求 | 全部成功，无 429 / 配额限流 |
| 第二条 Noise 连接（同账号同 Cookie） | 2.2s 建立成功；第一条连接不被踢、订阅不受影响 |

结论：**上游按 VM 并行执行 Agent 轮次，单账号的并发上限远高于网页端一次一轮的用法**。因此 0.8.0 把网关改成并发友好的结构：

- **连接池**：每个账号维护 `MUSE2API_PROTOCOL_CONNECTIONS`（默认 2）条 Noise 连接。新轮次分摊到最空闲的连接；线程固定在创建它的连接上（订阅与命令注册都是连接级的）。`MUSE2API_ACCOUNT_MAX_CONCURRENCY` 默认相应提高到 12。
- **同会话并行轮次不再互相破坏**：续接判定移到线程锁内（0.7.0 及之前，第二个请求会在第一个请求仍在流式输出时替换线程并关闭其订阅）。同一线程上的并发请求现在排队串行执行，全部成功。
- **稳定 `node_id`**：连接重连后沿用同一个设备标识，续接轮次与命令注册不会因为设备身份变化而丢失上游上下文。
- **Cookie 失效显式化**：连接建立时上游合并进来的 Cookie 不再触发其他连接重连（初版连接池在这里出现重连乒乓）；只有管理接口替换账号 Cookie 时才通过 `invalidate_account` 让连接在下次使用时重建。
- **预热**：`prewarm` 在启动时后台建好第一条连接，首个请求不再支付 3-20 秒握手。
- **`n>1` 图片并行**：一次生成多张图片时各轮并发执行。

### 3.2 异常路径与上游取消（0.9.0）

对 `/chat/cancel` 与轮次生命周期的逆向实测（probe 16，真实 muse.ai）：

| 场景 | 结果 |
|---|---|
| 轮次进行中 `POST /chat/cancel {session_id}` | 立即返回 `{"cancelled": true, "message_id": ...}`；无需 message_id |
| 取消后的事件流 | 2 秒内出现 `delta.message_done`，`status` 为 **`interrupted`**（不是 completed 也不是错误）；`text_append` 立即停止 |
| 并行轮次互斥性 | 取消其中一条并行轮次，另一条正常跑完，互不影响 |
| `delta.message_done` 载荷 | 携带完整 transcript 与 `metadata.model`，**不含 token 计数**（usage 仍是网关近似值） |

由此 0.9.0 修复了三类异常路径缺陷：

- **孤儿轮次**：0.8.0 及之前，`client.cancel` 是死代码——取消事件只在驱动生成器已被抛弃后才被设置。现在驱动在轮次未完成就退出时（客户端断开、总超时、首字超时换号、`stop` 截断）一律 best-effort 调用 `/chat/cancel` 并记录日志；网关也会立即关闭驱动生成器而不是等 GC。
- **线程污染**：被中止的轮次事件会继续追加到上游线程，下次订阅会从旧 seq 重放孤儿轮次的文本。现在中止即弃线程：从会话表移除并关闭订阅，下次同会话请求重开线程、重发全量历史。
- **死订阅**：Noise 传输中途断开时，订阅读取循环退出或永久空转。消费循环现在检测"订阅已关闭或连接已死且未收到完成事件"，立即报错换号，不再空转到 300 秒总超时。轮次之间被上游关闭的订阅会自动重开（从线程 seq 续播）。

`delta.message_done` 的 `status: "interrupted"` 被视为正常结束（部分文本就是答案）；只有其他失败状态才触发换号重试。图片/视频轮次同样在调用方消失时取消上游生成。

## 4. 系统提示词

muse.ai 的 `chat.stream` 没有 `system` 字段：发送的文本就是一条用户消息。0.7.0 起网关用两层机制逼近真正的 system 语义。

### 4.1 VM 托管规则（`MUSE2API_NATIVE_HARNESS`，默认开启）

对 muse.ai 前端与 VM 文件系统的逆向发现：Agent 在**每次会话开始时读取** `~/IDENTITY.md`、`~/SOUL.md`、`~/AGENTS.md`，之后服务端还会把它们作为上下文逐轮重新注入。这是 muse.ai 上最接近 system role 的通道。网关在这三个文件的末尾维护一段 `muse2api-managed` 标记包裹的规则（幂等、可还原）：

- API 会话（首条用户消息带【系统指令】）必须整段跟随指令定义的角色、语气、约束与输出格式；
- 禁止调用 `muse.memory_search` / `muse.memory_get`，不读 MEMORY.md / USER.md / memory 目录（实测：明确点名这些工具后，模型才会真正放弃账号记忆，不再把用户当作账号主人）；
- 已注册客户端命令的会话，匹配任务时优先调用客户端命令而不是内置浏览器/搜索工具。

原始文件在首次应用前备份到 `data/harness_backup/<account>/`；`GET /admin/harness` 查看状态，`POST /admin/harness/{account_id}/restore` 还原。

### 4.2 会话内指令块

1. `system` / `developer` 消息被收集成一个前置指令块，标记为最高优先级；
2. 当前用户消息单独放在最后，指令和问题分开；
3. 第一轮发送完整提示词；同一 `user` 的后续轮次只发送新的用户消息，前面的指令已经在线程里；
4. 系统提示词按空白归一化后生成指纹；指纹变化时驱动开新线程，避免新旧指令混用；
5. `MUSE2API_MAX_PROMPT_CHARS` 限制提示词长度，超长时优先保留末尾的对话与任务说明。

**0.7.0 实测（真实 muse.ai，经网关全链路）**：带 system 的会话中，模型自称角色名（"我是 Atlas-77，一个天气问答助手"）、回复格式精确跟随（两短句）、记忆隔离生效（不再泄露账号主人信息）；同线程续接保持规则；工具调用与结果续接正常。

**已知边界**（逆向确认，无法绕过）：muse.ai 的核心身份写在服务端静态提示词里（VM 内 `workspace/system/system_prompt.md`，73 KB，可读但没有写入口）。没有 system 消息、仅把身份写在普通用户文本里时，直接问"你是谁"仍会自称 Muse——这与 OpenAI 语义一致（user 消息弱于 system），不是网关缺陷。`hidden_prompt` 字段行为惰性；Shared Agents（自带 identity/definition 的真 system 通道）服务端已 404 下线；`goal_prompt` 返回 410；`chat.stream` 的 `model` 字段被接受但忽略；`temperature` / `top_p` 等采样参数上游不暴露（0.9.0 探针确认 `delta.message_done` 也不含 token 计数），网关按"接受但忽略"处理，JSON 模式用指令块 + 解析校验近似实现。

## 5. 工具调用（原生客户端工具通道 + Planner 兜底）

对 muse.ai 前端 JS 的逆向发现了真正的客户端工具通道：

```
POST /client/register-capabilities   声明 hatch_app_commands（参数 schema、超时）
     └─ linked_nodes: [{node_id}]    绑定到连接的稳定 node_id
订阅事件 client.invoke               Agent 决定调用 → {command_id, invoke_id, params_json}
POST /client/invoke-result           网关应答 {invoke_id, ok, payload_json|error}
```

关键事实（逆向 + 实测验证）：

- **0.7.0 修正**：`ui.navigate.*` 和 `ui.set.*` 两个命名空间都会被注入 Agent 的工具列表并路由；`api.*`/`device.*`/`hatch.*`/`web.*`/`client.*` 注册时全部被接受（`accepted_hatch_app_commands` 计数、`rejected_ids` 为空）但调用返回 `capabilities_not_declared`，永远不会路由；
- Agent 通过 `ui.list` 发现命令，调用时附带 `mode: perform|suggest|lightup`（网关会剥掉）；
- 注册时 `linked_nodes` 绑定 `node_id`，且 `chat.stream` 的 `node_id` 必须是同一个稳定 id（0.5.0 的每轮随机 uuid 收不到 invoke）；
- 命令声明必须**稳定存在**（连接生命周期内不撤销/重注册，否则 VM 侧不再注入）；
- 每个命令有 `timeout_ms`（网关注册 30s）；网关对捕获的调用立即应答 `{status:"dispatched"}`，对无关调用应答 `not_available`；
- **0.7.0**：一轮可以产生多个 `client.invoke`（Agent 顺序调用多条命令），驱动全部捕获并编码为 `{"calls":[...]}`，OpenAI 层展开成多条 `tool_calls`；
- `chat.stream` 的 `tool_call_prefill` 字段（Web 端用它预置 `browser.windows`）**不能**预置客户端命令，实测不产生 `client.invoke`。

混合通道（`MUSE2API_TOOL_CHANNEL=auto`，默认）：

1. 每次带 `tools` 的请求，网关把 OpenAI 工具转换为 `ui.navigate.<tool>` 命令注册；
2. 提示词在扁平 JSON Planner 的基础上加"真实工具"段：这些工具已真实注册，优先使用它们而不是内置浏览器/搜索工具，禁止说"没有这个工具"；
3. Muse 原生调用 → `client.invoke` 事件 → 网关捕获并重编码 → 标准 OpenAI `tool_calls`（支持多条）；
4. Muse 输出 JSON → 原 Planner 路径解析。两条路都产出标准 `tool_calls`，拒绝类回复（"没有该工具/命令未声明/查不到"）会触发 Planner 重试。

`MUSE2API_TOOL_CHANNEL=planner` 可退回纯 Planner。实测（真实 muse.ai，0.7.0 网关全链路）：

```
非流式 tool_calls       finish=tool_calls，{"city":"北京"} 参数正确
流式 tool_calls         finish=tool_calls，delta.tool_calls 正常
工具结果续接            正确引用返回值，格式规则保持
并行工具解析            {"calls":[...]} → 多条 tool_calls（单元测试覆盖）
```

保留的 0.5.0 改进：参数按 JSON Schema 归一化（数字 `123456` 会按 schema 转成 `"123456"`）；工具结果续接走同一线程；流式以标准 `delta.tool_calls` 输出，`finish_reason=tool_calls`。

## 6. 图片与视频

`POST /v1/images/generations`、`/v1/images/edits`、`/v1/videos` 都由同一个协议驱动实现：

- 提示词形如 `Generate an image (aspect ratio 1024x1024): ...`；
- 参考图作为 `image` item（base64）发送；大于 8 MB 时走 `fs.write` 上传为 `file_ref`；
- 从 `delta.presentation` / `delta.tool_done` 中递归提取 `workspace/...` / `sandbox://...` 资源；
- 用 `fs.read` 分块下载，按资源 MIME 或文件头返回；
- 图片实测 24.5 秒，视频实测约 52 秒（4 秒 1280×720 MP4）。

## 7. 配置

| 变量 | 默认 | 说明 |
|---|---|---|
| `MUSE2API_DRIVER` | `mock` | 设为 `protocol` |
| `MUSE2API_UPSTREAM_PROXY` | 空 | HTTP 引导 / WebSocket / `fs.read` 的代理；空时回退 `BROWSER_PROXY` |
| `MUSE2API_PROTOCOL_PING_INTERVAL` | `30` | `/api/ping` 心跳间隔秒数，0 关闭 |
| `MUSE2API_PROTOCOL_WAKE_CONFIRM` | `20` | VM 唤醒后轮询 `/api/session` 的最长秒数 |
| `MUSE2API_PROTOCOL_THREAD_TTL` | `21600` | 空闲线程保留秒数，0 永久 |
| `MUSE2API_PROTOCOL_MAX_THREADS` | `64` | 每账号线程上限，超出按 LRU 淘汰 |
| `MUSE2API_PROTOCOL_CONNECTIONS` | `2` | 每账号并行 Noise 连接数；并发轮次分摊其上，线程固定在自己的连接 |
| `MUSE2API_TOOL_CHANNEL` | `auto` | `auto`/`native`：原生客户端工具 + Planner 兜底；`planner`：只用扁平 JSON Planner |
| `MUSE2API_MAX_PROMPT_CHARS` | `18000` | 系统指令与历史的最大长度 |
| `MUSE2API_TOOL_RETRIES` | `2` | Planner 被拒绝时的重试次数 |
| `MUSE2API_NATIVE_HARNESS` | `true` | 在账号 VM 的 IDENTITY/SOUL/AGENTS.md 维护托管规则（备份在 `data/harness_backup/`，可用 `/admin/harness` 还原） |

## 8. 已知限制

1. **confidential VM**：只支持标准 `hatch_vm`。如果账号被分配到 confidential VM / delegation notary，驱动会返回明确错误，不会静默回退浏览器。
2. **上游行为**：muse.ai 仍是 Agent，不是可完全控制的 LLM；它可能拒绝某些请求，或给出与普通 LLM 不同的结果。
3. **账号级记忆**：会话隔离前导 + 托管规则明确禁用 `muse.memory_search` / `muse.memory_get`（0.7.0 实测有效）；账号的记忆文件本身仍会由上游的后台整理流程读写，网关不会删除或改写 MEMORY.md。
4. **线程订阅**：每个活跃线程一个长连接订阅；大量并发会话会占用上游订阅资源，默认 64 个/账号，按 LRU 淘汰。有轮次正在执行的线程不会被淘汰。
5. **系统提示词边界**：`system` 通过"托管规则 + 指令块"双层注入，实测能稳定跟随角色、格式与记忆隔离；但 muse.ai 服务端的静态核心提示词（VM 内 `workspace/system/system_prompt.md`）没有写入口，完全替换模型身份做不到。没有 system 消息时行为与 OpenAI 语义一致（user 文本弱于 system）。
6. **原生工具的随机性**：Muse 偶尔无视已注册命令直接说"没有这个工具"，或优先用自己的内置工具；混合通道用"真实工具"提示词 + 托管规则 + 拒绝检测 + Planner 重试兜底，上游行为不能 100% 保证。
7. **命令注册是连接级的**：同账号所有会话共享已注册命令；无关会话的 Muse 若自发调用会被 `not_available` 拒绝并自行解释。
8. **图片格式**：返回上游原始格式（常见 WebP），不强制转 PNG；`background=transparent` 仍需要可选 matting extra。
9. **托管规则影响整个账号**：IDENTITY.md/SOUL.md 的托管段对该账号在 muse.ai 网页端的手工使用也可见。需要原样使用网页端时，先 `POST /admin/harness/{id}/restore` 还原备份。

## 9. 部署与回滚

```bash
docker build -f Dockerfile.protocol -t muse2api-web:protocol-0.9.0 .
docker run -d --name muse2api-web \
  --restart unless-stopped \
  -p 18610:18610 \
  -v /vol2/1000/muse2api-web/data:/app/data:rw \
  -e MUSE2API_DRIVER=protocol \
  -e MUSE2API_DATA_DIR=/app/data \
  -e MUSE2API_PUBLIC_BASE=http://<host>:18610 \
  -e MUSE2API_UPSTREAM_PROXY=http://<proxy>:7893 \
  -e MUSE2API_API_KEY=... \
  -e MUSE2API_ADMIN_KEY=... \
  -e MUSE2API_ACCOUNT_MAX_CONCURRENCY=12 \
  -e MUSE2API_KEEPALIVE_ENABLED=true \
  -e MUSE2API_EMULATE_TOOLS=true \
  muse2api-web:protocol-0.9.0
```

回滚到浏览器驱动（保留旧容器）：

```bash
docker stop muse2api-web
docker rename muse2api-web muse2api-web-protocol
docker rename muse2api-web-0.4.1 muse2api-web
docker start muse2api-web
```
