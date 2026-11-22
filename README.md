# muse2api

> 把 muse.ai 网页里的 Agent，变成一个标准的 OpenAI API。

muse2api 是一个自托管的**网页反代网关**：对外提供 `/v1/chat/completions`、`/v1/responses`、`/v1/images/generations`、`/v1/videos` 这些标准的 OpenAI 接口，对内通过 muse.ai 网页端使用的原生 Noise 协议直连它的 Agent。

它不点浏览器、不往聊天框里粘贴提示词，而是直接使用 muse.ai 自己的会话、线程、附件、客户端工具和生图/生视频通道。任何 OpenAI SDK、聊天客户端或 Agent 框架，把 `base_url` 指过来就能用。

## 特性

- **纯协议驱动**：Noise XX + `chat.stream` + 线程订阅，不需要 Chromium。`Dockerfile.protocol` 镜像约 1.69 GB（含 rembg/pymatting、BiRefNet-lite 模型与 ffmpeg），冷启动只付一次握手（网关启动时预热）。
- **对外只说 OpenAI 协议**：流式 SSE、`tool_calls`、`response_format`、`stop`、`n>1`（含流式多选）、`max_tokens`、`stream_options`、`system_fingerprint`；404/405/校验错误一律 OpenAI 错误信封，响应带 `x-request-id` / `openai-version` / `openai-processing-ms`。调用方无法从信封形状上分辨它与 api.openai.com。
- **用量可解释**：`usage` 由本地 `tiktoken`（`o200k_base`，镜像内预缓存）计数，带上 `prompt_tokens_details` / `completion_tokens_details`；`max_tokens` 也按同一 tokenizer 精确截断，而不是按字符估算。muse.ai 不返回计费 token，这是唯一诚实的近似。
- **Stored Chat Completions**：`store: true` 的补全可以 `list` / `retrieve` / `update`（元数据）/ `delete`，并通过 `/messages` 读取输入消息；默认不存储，与 OpenAI 一致。
- **附件真的能读**：`file` / `input_file` / `input_audio` 内容部件会作为 `file_ref` 附件上传到账号 VM，Agent 读取文件内容后回答（逆向实测：从上传的 txt 里精确读回指定密语）。
- **网页对话里的产物不会丢**：Agent 在聊天中生成的图片、视频和文档（上游 `delta.presentation`）会被下载并托管到 `/v1/media`，以 Markdown 追加到回复里；网页检索读过的来源（`message_done.resources` 的 `citation`）会变成标准的 `url_citation` annotations。网页端能看到的东西，API 端同样能看到。
- **OpenAI Files / Uploads API**：`POST /v1/files` 上传一次，聊天与 Responses 用 `file_id` 引用；`/v1/files/{id}/content` 取回原文件，支持列表、检索、删除与过期清理。`/v1/uploads` 提供 64 MiB 分片续传，完成后自动生成普通 Files 对象。
- **Responses API 完整可用**：`previous_response_id` 有状态续聊（落盘，重启不丢）、`tools`/`tool_choice` 函数调用、完整流式事件序、`response.in_progress` / annotation 事件、`max_output_tokens` 截断语义，以及 `GET`/`DELETE /v1/responses/{id}`、`/input_items`、`/input_tokens`、`/compact`、`/cancel`。
- **Conversations API**：`/v1/conversations` 的创建、检索、改元数据、删除与 items 分页；Responses 传 `conversation=...` 会自动前置历史并在完成后追加本轮，等价于官方的有状态会话。
- **系统提示词生效**：`system` / `developer` 消息走"VM 托管规则 + 会话内指令块"两层注入，模型会真正跟随角色、格式与约束。
- **工具调用走原生通道**：OpenAI `tools` 注册成 muse.ai 的原生客户端命令，Agent 在自己的工具循环里调用，再还原成标准 `tool_calls`；Planner 兜底，支持 legacy `functions` / `function_call` 与 `parallel_tool_calls`。
- **内置网页搜索**：`web_search_options`（Chat）与 `tools: [{"type": "web_search"}]`（Responses）会驱动 muse.ai 自己的网页搜索/读取工具，返回真实来源引用，而不是伪造搜索结果。
- **单账号真并发**：逆向实测上游按 VM 并行执行轮次（单连接 12 条并发流零错误），网关用连接池 + 每账号 12 并发把这份能力交给调用方。
- **异常路径干净**：客户端断开、超时、换号重试都会真正取消上游正在执行的轮次，被中止的线程直接丢弃，不会把半截回复泄漏进下一轮。
- **多账号、多会话**：账号池负责调度、冷却与故障转移；会话按 API Key 隔离，热线程复用降低首字延迟。
- **多 Key 数据隔离**：`/v1/files`、Conversations、stored completions、Responses 链与图片任务都带 owner；一把 Key 不能列出、下载、续聊或删除另一把 Key 的数据。
- **图片与视频**：文生图、参考图、异步任务、文生视频、透明背景抠图都在同一个网关里；图片支持 `output_format`（png/jpeg/webp）与 `output_compression` 本地转码。视频接口按 OpenAI Sora 的形状提供 `POST /v1/videos`（JSON 或 multipart）、检索、分页列表、`/content` 下载（`variant=video|thumbnail|spritesheet`）、`/remix`、删除，状态为 `queued` / `in_progress` / `completed` / `failed`。
- **自带控制台**：账号池、API 密钥、请求日志与流量统计；界面沿用 muse.ai 的视觉语言（米白画布、药丸控件、克制的蓝色、轻量动效），浅色/深色/跟随系统，移动端可用，零外部依赖。

## 快速开始

### Docker（推荐）

```bash
cp .env.example .env          # 按需修改
docker compose up -d --build  # 浏览器驱动镜像（含 Chromium）
```

纯协议驱动更轻，直接构建协议镜像：

```bash
docker build -f Dockerfile.protocol -t muse2api:protocol .
docker run -d --name muse2api \
  --restart unless-stopped \
  -p 18610:18610 \
  -v "$PWD/data:/app/data" \
  -e MUSE2API_DRIVER=protocol \
  -e MUSE2API_UPSTREAM_PROXY=http://<proxy>:7893 \
  muse2api:protocol
```

服务默认监听 `127.0.0.1:18610`。首次启动如果没有设置 `MUSE2API_API_KEY`，会自动生成一个密钥并保存到 `data/api_key`。

### 本地运行

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e '.[dev]'
python -m muse2api
```

默认的 `mock` 驱动不需要账号、不访问网络，适合先跑通接口与控制台。

### 放入一个 muse.ai 账号

从已登录 muse.ai 的浏览器导出 Cookie，至少需要 `hatch_sess` 和 `hatch_native_auth_device`：

```bash
curl http://localhost:18610/admin/accounts \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"label":"main","cookies":{"hatch_sess":"...","hatch_native_auth_device":"..."}}'
```

也可以打开 `http://localhost:18610/`，在「账号池」页下载 Chrome 插件，一键读取 httpOnly Cookie 并导入；「批量导入」支持一次粘贴多行。

### 客户端配置

| 字段 | 值 |
|---|---|
| API Base | `http://<网关地址>:18610/v1` |
| API Key | `MUSE2API_API_KEY`，或控制台创建的 `m2a-...` 密钥 |
| 模型 | `muse-chat`、`muse-image`、`muse-video`（其他模型名一律拒绝） |
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

浏览器端应用（Open WebUI、LobeChat web、LibreChat 等）也可以直接调用：`/v1/*` 默认返回 `Access-Control-Allow-Origin: *`，与 api.openai.com 一致。需要收紧时设置 `MUSE2API_CORS_ORIGINS=逗号分隔的来源列表`。

## 控制台

打开 `http://<host>:18610/`，输入管理密钥（`MUSE2API_ADMIN_KEY`，未设置时回退到 `data/api_key`）。页面是单文件、无外部依赖，密钥只保存在浏览器的 localStorage。

![muse2api 控制台](docs/images/dashboard-overview.png)

- **总览**：请求总数、错误率、P95 延迟、可用账号、密钥用量；请求趋势图与密钥/账号/模型/状态码分布，支持 1 小时 / 24 小时 / 7 天。
- **账号池**：每个账号一张卡片，显示状态、成功/失败计数、额度进度、Cookie 过期时间与最近错误；操作包括续期、重置、看请求、刷新额度、启禁用、删除。支持批量导入与插件下载。
- **API 密钥**：创建、重命名、吊销、恢复、删除；每个密钥的请求量、错误数与最近使用时间。明文只在创建时显示一次。
- **请求日志**：按密钥、账号、状态、路径过滤，自动刷新与分页；点击一行查看完整字段（模型、耗时、流式、任务状态、客户端 IP、User-Agent）。

界面沿用 muse.ai 的视觉语言：米白画布、药丸形控件、单一蓝色强调，以及数字滚动、图表描边、页面淡入等轻量动效。提供"跟随系统 / 浅色 / 深色"三种主题；窄屏下侧栏收进抽屉，表格自动折叠成卡片。

## 请求是怎么走的

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

1. 路由校验模型名，把多轮 `messages` 与 `system` 指令整理成一条提示词；
2. 网关从账号池租一个账号，驱动建立或复用 muse.ai 线程；
3. 上游的 `delta.text_append` 直接变成 OpenAI `delta.content`；
4. 出错时按类型更新账号状态，在尚未向客户端发送内容前自动换号重试；
5. 流式请求先拿到第一个 chunk 再返回 200，避免"中途断流"。

## 兼容层细节

### 系统提示词

muse.ai 的 `chat.stream` 没有独立的 `system` 角色。网关用四层机制逼近真正的 system 语义：

1. **VM 托管规则**：Agent 每次会话开始都会读取账号 VM 里的 `IDENTITY.md` / `SOUL.md` / `AGENTS.md`，服务端还会逐轮重新注入。网关在这三个文件末尾维护一段带标记的托管规则（跟随 system 指令、禁用账号记忆工具、优先使用已注册客户端命令）。原文件先备份到 `data/harness_backup/`，`POST /admin/harness/{id}/restore` 可随时还原。
2. **会话内指令块（0.17.1 重写投递）**：`system` / `developer` 消息以“这段对话请始终遵循以下设定”的自然请求措辞随首条消息投递，当前用户消息单独放在最后；后续轮次只发送新的用户轮。指令内容变化会换新线程，避免新旧规则混用。0.17.1 起不再使用「系统指令｜最高优先级」类自我声明式包装——上游 2026-11-17 新构建（新增对齐体系）会把这类包装整体识别为 prompt injection 丢弃（probe 59/66/67），而 plain 措辞命中其核心提示词的明文豁免（"adopt user requested personalities and perspectives faithfully"），格式/约束/语气类指令恢复可靠遵循。
3. **人格自介绍缓解（0.17.2，probe 70-73）**：对齐时代的模型对"你是谁/自我介绍"类身份问句一律如实答 Muse（few-shot 种子、渲染指令、任务重映射均无法翻转——已逐一实测）。`MUSE2API_PERSONA_INTRO=true`（默认开）时，会话的 system 提示词命名了角色（"你是 X"）且用户发送**独立的身份问句**时，网关将该轮投递为已验证的"为「X」这个角色写台词"创作任务（probe 73：T 3/3 + U 2/2），回复即为人格的自介绍台词。仅整句身份问句触发，不影响其他消息。
3. **原生 persona 语气通道（0.16.0 probe 19 / 0.17.0 probe 21-26）**：`POST /admin/identity/{id}` 走上游 `onboarding.identity.set`，`vibe`（`dry_witty` / `short_concise` / `warm_friendly`）写进 `IDENTITY.md` 后被服务端**逐轮实时**解析注入（probe 23b：同一线程中途删掉该行，语气当轮回退）。`vibe="none"` 删除该行（上游没有 vibe 删除接口，这是唯一的关闭开关）；`GET /admin/identity/{id}` 附带 `gateway.vibe_line` 诊断（probe 25：自由文本值会被注入器的 id 校验丢弃）。`name` 仅注册期可写（probe 22/26：重开 bootstrap 也不解锁，PATCH/POST /identity 均 403）——模型还会主动核对服务端身份记录、拒绝文件里未登记的人格声明（probe 24）。
4. **网页同源信封（0.16.0，probe 18）**：连接时 `model.get`（`GET /model`）读取账号选中模型与 `model_identity_visibility`，`visible` 时每轮 `chat.stream` 携带 `model`；发送网页客户端同款 `capabilities`；`MUSE2API_TIMEZONE` 使上游逐轮渲染 `[Day YYYY-MM-DD HH:MM:SS TZ] [client_timezone=…]` 开发者消息（时区粘性保持）。`GET/PATCH /admin/vm-config/{id}` 可读写 VM 推理努力配置（`llm.reasoning.*.effort`）。
5. **隐私开关（0.17.0，probe 21）**：`GET/POST /admin/product-improvements/{id}` 读写账号的 Meta 数据训练授权（`training_enabled`）。给网关接入第三方提示词前，可先 `{"training_enabled": false}` 退出训练。完整注入面全图（含已证实的硬边界）见 [docs/PROTOCOL.md](docs/PROTOCOL.md)。

实测（真实 muse.ai 全链路）：带 system 的会话里，模型自称 system 定义的姓名、精确跟随输出格式、不泄露账号主人的记忆。回复里的网页版内联引用标记（`【url_id†L9】`，网页 UI 渲染成来源卡片）可用 `MUSE2API_CITATION_MARKERS=footnote|strip` 归一化为 `[1]` 脚注或移除，URL 本体始终在 `annotations`。完整逆向记录见 [docs/PROTOCOL.md](docs/PROTOCOL.md)。

### 工具调用

请求带 `tools` 时，网关把 OpenAI 工具注册成 muse.ai 原生的 `ui.navigate.<工具名>` 客户端命令：

1. Muse 在自己的工具循环里调用命令，网关收到 `client.invoke` 事件（一轮多条调用也会全部捕获）；
2. 网关立即应答避免超时，并把调用重编码为标准 `tool_calls`；
3. 客户端把工具结果以 `role: "tool"` 发回来，网关续接同一线程继续回答；
4. Planner 作为兜底输出契约，模型走哪条路径都能产出 `tool_calls`。

`MUSE2API_TOOL_CHANNEL=auto`（默认）优先原生通道；`native` 只用原生；`planner` 只用扁平 JSON Planner。流式请求返回标准 `delta.tool_calls`，`finish_reason` 为 `tool_calls`。

### 文件与音频

chat completions 与 Responses 都接受附件，内容会作为 `file_ref` 附件发送给上游：

```python
resp = client.chat.completions.create(
    model="muse-chat",
    messages=[{"role": "user", "content": [
        {"type": "text", "text": "附件里约定的暗号是什么？"},
        {"type": "file", "file": {
            "file_data": "data:text/plain;base64,...",   # 也支持 PDF 等任意 MIME
            "filename": "notes.txt"}},
    ]},
)
```

音频用 `{"type": "input_audio", "input_audio": {"data": "<base64>", "format": "wav"}}`。

也支持标准的「先上传、后引用」流程（官方 SDK 直接可用）：

```python
file = client.files.create(
    file=("notes.txt", open("notes.txt", "rb")), purpose="assistants")
resp = client.chat.completions.create(
    model="muse-chat",
    messages=[{"role": "user", "content": [
        {"type": "text", "text": "附件里约定的暗号是什么？"},
        {"type": "file", "file": {"file_id": file.id}},
    ]},
)
```

`file_id` 会在请求进入提示词前展开成内联 data URL；`input_file` / `input_image` 的 `file_id` 同样可用。上传文件保存在 `data/files`，默认保留 30 天（`MUSE2API_FILES_RETENTION_DAYS`）。

大文件可以走标准 Uploads 分片流程（0.13.0）：

```python
upload = client.uploads.create(
    bytes=os.path.getsize("big.pdf"), filename="big.pdf",
    mime_type="application/pdf", purpose="user_data")
part_ids = []
with open("big.pdf", "rb") as fh:
    while chunk := fh.read(64 * 1024 * 1024):
        part_ids.append(client.uploads.parts.create(upload.id, data=chunk).id)
file = client.uploads.complete(upload.id, part_ids=part_ids).file
```

完成后返回普通 Files 对象，后续用 `file.id` 引用即可；分片默认只在上传未完成时保留，Upload 本身 1 小时后过期。

### 网页对话里的图片、视频与引用

muse.ai 的 Agent 会在聊天中间直接生成图片/视频/文档，也会在网页搜索后附带来源。这些事件原本只属于网页端，网关现在把它们翻译成标准 API 形态：

- 生成的图片以 `![说明](http://<网关>/v1/media/chat_xxx.webp)` 追加到 `message.content`，视频/文档以 Markdown 链接追加；文件由网关重新托管，不依赖账号 VM 的临时域名。
- 网页搜索读过的来源变成 `message.annotations`（Chat）或 `output_text.annotations`（Responses）里的 `url_citation`；流式 Responses 还会发 `response.output_text.annotation.added` 事件。
- 用 `web_search_options`（Chat）或 `tools: [{"type": "web_search"}]`（Responses）可以显式要求联网检索；不传时如果 Agent 自行浏览，引用同样会带回来。

### Responses API

`/v1/responses` 与 OpenAI 语义对齐：

- **有状态**：响应默认存储到 `data/responses.json`，后续请求带 `previous_response_id` 即自动续上整段对话（TTL `MUSE2API_RESPONSE_TTL`，默认 7 天）；也可以创建 `/v1/conversations` 并传 `conversation=...`，网关会前置该会话的 items 并在完成后追加本轮；`store: false` 不落响应存储（Conversation 仍会记录本轮）；未知 id 返回 404。
- **函数调用**：`tools` / `tool_choice` 走与 chat completions 相同的规划器，输出标准 `function_call` 项；`function_call` / `function_call_output` 可作为输入项回传续接；流式有 `response.function_call_arguments.delta/done` 事件。同时接受 Chat 的嵌套形状和 Responses 的顶层函数工具形状。
- **完整事件序**：`response.created` / `response.in_progress` / `output_text.done` / `content_part.done` / annotation 事件补齐；`max_output_tokens` 触发 `status: "incomplete"` 与 `response.incomplete`；流式错误使用 Responses 的 `{"type": "error", ...}` 事件。
- **完整对象面**：`GET /v1/responses/{id}`、`DELETE`、`/input_items`（分页，默认 `desc`）、`/input_tokens`（不生成、只计数）、`/compact`（把长会话压成可回传的 compaction item）、`/cancel`（仅进行中的请求）。
- **当前对象字段**：`completed_at`、`service_tier`、`access_programs`、`background`、`conversation`、`max_tool_calls`、`top_logprobs`、`safety_identifier`、`prompt_cache_key`，以及 `usage.input_tokens_details.cache_write_tokens` 与 `output[*].content[*].logprobs`；进行中的响应可被检索，取消后会持久化为 `cancelled`。
- `metadata` / `user` / `parallel_tool_calls` / `tool_choice` / `reasoning` / `truncation` 会原样回显在响应对象里；`metadata` 按 OpenAI 的 16 对 / 64 / 512 字符边界校验。
- `previous_response_id` 续聊时**永远不继承**上一轮的 `instructions`（OpenAI 语义），本轮自己的 instructions 才会生效。
- 无法表达的参数明确返回 400 而不是静默忽略：`background`（上游轮次绑定在启动它的 WebSocket 上，没有可恢复的服务端队列）、`top_logprobs`、非 `auto`/`default` 的 `service_tier`。

Conversation 用法（官方 SDK 直接可用）：

```python
conv = client.conversations.create(metadata={"trace": "demo"})
client.responses.create(model="muse-chat", input="第一轮", conversation=conv.id)
client.responses.create(model="muse-chat", input="第二轮", conversation=conv.id)
client.conversations.items.list(conv.id)   # 分页查看本轮各方 items
client.conversations.delete(conv.id)
```

长会话可以压缩成可回传的 compaction item：

```python
compacted = client.responses.compact(model="muse-chat", input="很长的历史……")
client.responses.create(
    model="muse-chat",
    input=[compacted.output[0], {"role": "user", "content": "继续"}])
```

### 参数支持

| 参数 | 行为 |
|---|---|
| `response_format: json_object / json_schema` | 注入 JSON-only 指令块；产出剥围栏并用 `json.loads` 校验，不合格换线程重试（`MUSE2API_TOOL_RETRIES`） |
| `stop` | 命中即在序列前截断并结束流式，上游轮次同时取消 |
| `n > 1` | 非流式：并发独立补全合并为多个 `choices`；流式：按到达序交错输出带 `index` 的 chunk |
| `max_tokens` / `max_completion_tokens` / `max_output_tokens` | 用 `tiktoken o200k_base` 精确截断到 token 上限，`finish_reason: "length"`（Responses：`status: "incomplete"`）；`max_completion_tokens` 优先 |
| `stream_options.include_usage` | 每个 chunk 带 `usage: null`，末尾追加 choices 为空的 usage chunk（仅 `stream: true` 可用，否则 400） |
| `tools` / `tool_choice` / `parallel_tool_calls` | 函数调用规划；`parallel_tool_calls: false` 时每轮最多返回一个调用 |
| `functions` / `function_call` | legacy 函数调用形状，自动映射到现代 `tools` / `tool_choice` |
| `web_search_options` / `{"type": "web_search"}` | 驱动上游原生网页搜索并返回 `url_citation` |
| `store` | `true` 时保存补全，可经 `/v1/chat/completions[/{id}[/messages]]` 检索；默认 `false` |
| `service_tier` | 接受 OpenAI 的 `auto` / `default` / `flex` / `scale` / `priority` / `fast`，响应回显实际执行层级 `default`（上游没有分级队列） |
| `metadata` | 16 对以内、键 ≤64 / 值 ≤512 字符的字符串字典，原样回显 |
| `modalities` / `audio` | 请求音频输出时明确 400（muse 只回文本） |
| `logprobs` / `top_logprobs` | 明确返回 400（muse 模型不提供 token logprobs），不静默伪造 |
| `temperature` / `top_p` 等采样参数 | 接受但忽略——muse.ai 不暴露采样控制 |
| 未知 `model` | 404 `model_not_found`（`param: "model"`） |

## 接口

所有 `/v1/*` 使用 `Authorization: Bearer <API_KEY>`；`/admin/*` 使用 `MUSE2API_ADMIN_KEY`（未设置时回退到 API Key）。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` · `/readyz` | 存活 / 就绪检查 |
| GET | `/v1/models` · `/v1/models/{id}` | 模型列表 / 单个模型（仅 muse-chat / muse-image / muse-video） |
| POST | `/v1/files` · GET `/v1/files[/{id}[/content]]` · DELETE | OpenAI Files API：上传、列表、检索、下载、删除 |
| POST | `/v1/uploads` · `/{id}/parts` · `/{id}/complete` · `/{id}/cancel` | OpenAI Uploads API：分片上传，完成后生成普通 Files 对象 |
| POST | `/v1/chat/completions` | 聊天：流式、图片/文件/音频输入、工具调用、JSON 模式、网页搜索、`n>1`、stored completions |
| GET/POST/DELETE | `/v1/chat/completions[/{id}[/messages]]` | 列出 / 检索 / 改元数据 / 删除已存储的补全（`store: true`） |
| POST | `/v1/responses` | OpenAI Responses API（有状态、函数调用、网页搜索、annotations） |
| GET/DELETE | `/v1/responses/{id}` | 检索 / 删除已存储的响应 |
| GET | `/v1/responses/{id}/input_items` | 分页读取该响应的输入项（默认 `desc`） |
| POST | `/v1/responses/input_tokens` · `/compact` · `/{id}/cancel` | 输入 token 计数 / 压缩会话 / 取消进行中的响应 |
| POST/GET/DELETE | `/v1/conversations[/{id}]` | OpenAI Conversations API：创建、检索、改元数据、删除 |
| GET/POST | `/v1/conversations/{id}/items` | 分页读取 / 追加会话 items |
| GET/DELETE | `/v1/conversations/{id}/items/{item_id}` | 检索 / 删除单个 item |
| POST | `/v1/images/generations` · GET `/{id}` | 文生图；`"async": true` 返回任务，`"stream": true` 返回 `image_generation.completed` SSE |
| POST | `/v1/images/edits` | multipart 参考图 + 提示词 |
| POST | `/v1/videos` | Sora 形状创建（JSON 或 multipart，含 `input_reference`） |
| GET | `/v1/videos` · `/{id}` · `/{id}/content` | 分页列表 / 检索 / 下载视频（`variant=video|thumbnail|spritesheet`） |
| POST/DELETE | `/v1/videos/{id}/remix` · `/{id}` | 基于原视频重制 / 删除 |
| GET | `/v1/media/{name}` | 下载生成的媒体（含聊天内生成的图片/视频/文档） |
| GET/POST/PATCH/DELETE | `/admin/accounts[/{id}]` | 账号增删改查 |
| POST | `/admin/accounts/import` | 批量导入，逐条返回结果 |
| POST | `/admin/accounts/{id}/renew` · `/reset` | 续期 / 重置 |
| GET | `/admin/accounts/{id}/quota[?refresh=1]` | 额度快照 |
| GET/POST/PATCH/DELETE | `/admin/keys[/{id}]` | API Key 管理 |
| GET | `/admin/requests` · `/admin/stats` | 请求日志与聚合 |
| GET | `/admin/status` · `/admin/tasks` | 服务状态 / 任务列表 |
| GET/POST | `/admin/harness[/{id}/restore]` | 查看 / 还原 VM 托管规则 |
| GET/POST | `/admin/identity/{id}` | 原生身份读取（含 vibe_line 诊断）/ persona 语气写入与清除（`vibe="none"`） |
| GET/PATCH | `/admin/vm-config/{id}` | VM 推理配置读取 / 更新（`llm.reasoning.*.effort`） |
| GET/POST | `/admin/product-improvements/{id}` | Meta 数据训练授权读取 / 退出（`training_enabled`） |
| GET | `/admin/extension.zip` | Cookie 导入插件 |
| GET | `/` | 管理控制台（`/dashboard` 自动跳转） |

## 图片与视频

```bash
# 文生图
curl http://localhost:18610/v1/images/generations \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"prompt":"雨夜的赛博朋克街道","size":"16:9"}'

# 参考图（最多 4 张；字段名 image / images / image_url / reference_image 等均可，
# 支持 data URL、http(s) 链接、裸 base64（含换行/URL-safe）；也可用 /v1/images/edits 上传文件）
curl http://localhost:18610/v1/images/generations \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"prompt":"模特穿着这件卫衣，棚拍","image":"data:image/jpeg;base64,..."}'

# 指定输出格式与压缩率（png / jpeg / webp）
curl http://localhost:18610/v1/images/generations \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"prompt":"一只红狐狸","output_format":"webp","output_compression":80}'

# 异步图片（避免代理超时）
curl http://localhost:18610/v1/images/generations \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"prompt":"一只红狐狸","async":true}'

# 流式图片（OpenAI gpt-image-1 形状；new-api 等中转默认会带 stream:true）
# 返回 SSE：event: image_generation.completed，data 里带 b64_json（url 模式下另带 url）
curl -N http://localhost:18610/v1/images/generations \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"prompt":"一只红狐狸","stream":true}'

# 文生视频（Sora 形状；也接受 multipart 与 input_reference）
curl http://localhost:18610/v1/videos \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"sora-2","prompt":"枫叶在微风中飘落","seconds":"5","size":"1280x720"}'

# 轮询状态 → 下载内容
curl http://localhost:18610/v1/videos/$VIDEO_ID -H "Authorization: Bearer $KEY"
curl -L http://localhost:18610/v1/videos/$VIDEO_ID/content \
  -H "Authorization: Bearer $KEY" -o video.mp4

# 缩略图 / 拼图（本地 ffmpeg，结果缓存在 data/media）
curl -L "http://localhost:18610/v1/videos/$VIDEO_ID/content?variant=thumbnail" \
  -H "Authorization: Bearer $KEY" -o thumb.png
curl -L "http://localhost:18610/v1/videos/$VIDEO_ID/content?variant=spritesheet" \
  -H "Authorization: Bearer $KEY" -o sheet.png
```

Stored Chat Completions：

```python
resp = client.chat.completions.create(
    model="muse-chat", store=True,
    messages=[{"role": "user", "content": "记住暗号 XYZZY"}],
    metadata={"trace": "demo"})
client.chat.completions.retrieve(resp.id)
client.chat.completions.messages.list(resp.id)   # 输入消息
client.chat.completions.list(metadata={"trace": "demo"})
client.chat.completions.update(resp.id, metadata={"trace": "renamed"})
client.chat.completions.delete(resp.id)
```

`background: "transparent"` 会在本地用 BiRefNet 抠图并返回 RGBA PNG。`Dockerfile.protocol` 已安装 `[matting]` extra 并内置 `birefnet-general-lite`（约 224 MB，缓存于 `/app/model-cache`）；本地开发用 `pip install -e '.[matting]'`，模型首次使用下载到 rembg 缓存（默认 `~/.rembg`，可用 `U2NET_HOME` 覆盖）。

## 配置

配置来自环境变量（前缀 `MUSE2API_`）或 `.env`，完整列表见 [.env.example](.env.example)。

**服务与存储**

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MUSE2API_HOST` · `MUSE2API_PORT` | `127.0.0.1` · `18610` | 监听地址与端口 |
| `MUSE2API_API_KEY` | 自动生成 | `/v1/*` 密钥 |
| `MUSE2API_ADMIN_KEY` | 同 API Key | `/admin/*` 密钥 |
| `MUSE2API_PUBLIC_BASE` | 空 | 对外访问地址（媒体链接使用） |
| `MUSE2API_CORS_ORIGINS` | `*` | 允许跨域调 `/v1/*` 的来源，逗号分隔 |
| `MUSE2API_DATA_DIR` | `data` | 数据目录 |
| `MUSE2API_REQUEST_LOG_RETENTION_DAYS` | `14` | 请求日志保留天数 |
| `MUSE2API_MEDIA_RETENTION_DAYS` | `14` | 媒体文件保留天数，`0` 关闭清理 |
| `MUSE2API_RESPONSE_TTL` | `604800` | Responses 会话可续聊的秒数 |
| `MUSE2API_FILE_MAX_BYTES` | `104857600` | `/v1/files` 与 `/v1/uploads` 单文件上限（100 MiB） |
| `MUSE2API_FILES_RETENTION_DAYS` | `30` | 上传文件保留天数，`0` 关闭清理 |
| `MUSE2API_CHAT_MEDIA` | `true` | 是否把聊天内生成的图片/视频/文档重新托管并追加到回复 |
| `MUSE2API_CHAT_MEDIA_MAX_BYTES` | `33554432` | 聊天内产物下载上限（32 MiB），`0` 不限制 |

**驱动**

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MUSE2API_DRIVER` | `mock` | `mock` / `protocol` / `browser` / `http` |
| `MUSE2API_UPSTREAM_PROXY` | 空 | 协议连接、续期、VM 唤醒的代理 |
| `MUSE2API_BROWSER_PROXY` | 空 | 浏览器驱动专用代理 |
| `MUSE2API_CHROMIUM_PATH` · `MUSE2API_CDP_PORT` | 空 · `19210` | 浏览器驱动：Chromium 路径与调试端口 |
| `MUSE2API_HEADLESS` | `true` | 浏览器驱动是否无头 |
| `MUSE2API_WARM_THREADS` · `MUSE2API_WARM_PROMPT` | `2` · `Hi` | 浏览器驱动预热线程 |

**协议驱动**

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MUSE2API_PROTOCOL_CONNECTIONS` | `2` | 每账号并行 Noise 连接数 |
| `MUSE2API_PROTOCOL_PING_INTERVAL` | `30` | `/api/ping` 心跳间隔秒数，`0` 关闭 |
| `MUSE2API_PROTOCOL_WAKE_CONFIRM` | `20` | VM 唤醒后轮询的最长秒数 |
| `MUSE2API_PROTOCOL_THREAD_TTL` | `21600` | 空闲线程保留秒数，`0` 永久 |
| `MUSE2API_PROTOCOL_MAX_THREADS` | `64` | 每账号线程上限（LRU 淘汰） |

**网页对话同源信封（0.16.0）**

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MUSE2API_CHAT_WIRE_PARITY` | `true` | `chat.stream` 与网页客户端逐字段一致：连接时 `model.get` 读取账号选中模型（`visible` 时每轮携带）、发送网页端同款 `capabilities` 列表 |
| `MUSE2API_TIMEZONE` | 空 | 每轮随请求发送的 IANA 时区；上游据此渲染 `[Day … TZ] [client_timezone=…]` 开发者消息且粘性保持，空 = 沿用账号已有值 |
| `MUSE2API_CITATION_MARKERS` | `native` | 回复内联引用标记（`【url_id†L9】`）的处理：`native` 原样 / `footnote` 重写为 `[1]/[2]/…` / `strip` 删除；URL 已在 `annotations` |
| `MUSE2API_PERSONA_INTRO` | `true` | 人格自介绍缓解：persona 会话中的独立身份问句按已验证的"角色台词"任务投递（0.17.2） |

**超时与心跳**

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MUSE2API_CHAT_TIMEOUT` | `300` | 聊天总超时（秒） |
| `MUSE2API_FIRST_TOKEN_TIMEOUT` | `75` | 首 token 超时（秒） |
| `MUSE2API_STREAM_HEARTBEAT_INTERVAL` | `0` | SSE 心跳间隔，`0` 关闭 |
| `MUSE2API_IMAGE_TIMEOUT` · `MUSE2API_VIDEO_TIMEOUT` | `240` · `600` | 媒体生成超时（秒） |

**账号池与工具**

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MUSE2API_POOL_STRATEGY` | `affinity` | `lru` / `round_robin` / `affinity` |
| `MUSE2API_ACCOUNT_MAX_CONCURRENCY` | `12` | 每账号并行请求数 |
| `MUSE2API_POOL_ACQUIRE_TIMEOUT` | `60` | 等待可用账号的秒数 |
| `MUSE2API_ACCOUNT_COOLDOWN` | `120` | 错误后的冷却秒数 |
| `MUSE2API_MAX_FAILOVER` | `2` | 换账号重试次数 |
| `MUSE2API_MAX_PROMPT_CHARS` | `18000` | 系统指令与历史的最大长度 |
| `MUSE2API_EMULATE_TOOLS` | `true` | 是否启用工具调用 |
| `MUSE2API_TOOL_CHANNEL` | `auto` | `auto` / `native` / `planner` |
| `MUSE2API_TOOL_RETRIES` | `2` | 工具/JSON 被拒绝时的重试次数 |
| `MUSE2API_NATIVE_HARNESS` | `true` | 维护 VM 托管规则（备份可还原） |
| `MUSE2API_KEEPALIVE_ENABLED` · `MUSE2API_KEEPALIVE_INTERVAL` | `true` · `600` | 后台续期与 VM 唤醒 |

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
- 反向代理若有约 100 秒限制，图片生成请使用 `async` 任务。

数据都在 `data/`：`accounts.json`、`keys.json`、`requests.db`、`responses.json`、`conversations.json`、`chat_completions.json`、`tasks.json`、`files.json`、`uploads.json`、`media/`、`files/`、`uploads/`、`harness_backup/`。升级前备份这个目录即可。

## 已知限制

1. muse.ai 是 Agent，不是可完全控制的 LLM；它仍可能拒绝某些请求，或输出与普通 LLM 不同的结果。
2. `temperature` / `top_p` 等采样参数接受但忽略；muse.ai 不返回计费 token，`usage` 与 `max_tokens` 由本地 `tiktoken` 计数/截断，与上游真实用量可能有偏差。
3. `response_format` 的 JSON 模式靠指令块 + 解析校验执行，极端情况下可能返回未通过校验的原始文本。
4. 没有 `system` 消息、只用普通用户文本描述身份时，直接问"你是谁"仍可能自称 Muse——这与 OpenAI 语义一致（user 弱于 system）。
5. 账号级记忆在 API 会话中已被托管规则禁用（实测有效），但网关不删除、不改写上游账号的 MEMORY.md。
6. 历史图片只保留占位符，跨线程依赖完整图片细节时可能丢失。
7. 视频的 `edits` / `extensions` / `characters` 返回 501（上游只提供生成与重制）；`background` Responses、音频输出、图片流式生成、`logprobs` 明确 400，不静默伪造。
8. 单账号默认 12 并发（上游实测无压力）；要更高并发建议增加账号，而不是继续调大这个值。

## 开发与测试

```bash
pip install -e '.[dev]'
pytest              # 单元与集成测试（mock 驱动，不访问网络）
ruff check .
```

## 来路

这个项目的协议层、账号池和管理台不是凭空长出来的。它站在两个开源项目的工作之上：

- **[www222fff/muse2api](https://github.com/www222fff/muse2api)**：本项目的起点。OpenAI 协议层、账号池、驱动分层、管理后台和 Cookie 导入插件等基础实现来自这里。
- **[lichao199208/muse2api](https://github.com/lichao199208/muse2api)**：纯协议反代、号池调度与任务接口的另一条实现路线，本项目的协议层设计从中获得参考。

也感谢 [muse.ai](https://muse.ai) 提供了这样一个能力完整的网页 Agent。

更细的文档：

- [docs/PROTOCOL.md](docs/PROTOCOL.md)：Noise 握手、线程、附件通道、系统提示词、工具通道
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)：分层与请求生命周期
- [docs/CLIENTS.md](docs/CLIENTS.md)：OpenAI 客户端接入

## 许可

[MIT](LICENSE)

本项目仅用于学习、技术研究与自托管实践。请遵守 muse.ai 的服务条款和当地法律，不要用于商业转售、批量注册或其他滥用行为。切勿提交或公开账号 Cookie。
