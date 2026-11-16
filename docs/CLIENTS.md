# OpenAI 客户端接入

muse2api 对外只说 OpenAI 协议。任何 OpenAI SDK、聊天客户端或 Agent 框架，把 `base_url` 指过来就能用；网关不会识别某个特定平台，也没有平台专用的特殊分支。

## 基本配置

| 字段 | 值 |
|---|---|
| API Base | `http://<host>:18610/v1` |
| API Key | `MUSE2API_API_KEY`，或控制台创建的 `m2a-...` 密钥 |
| 模型 | `muse-chat` / `muse-image` / `muse-video`（其他模型名一律拒绝） |
| 超时 | 建议 ≥ 240 秒（muse.ai 冷启动较慢） |

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:18610/v1", api_key="<API_KEY>")
resp = client.chat.completions.create(
    model="muse-chat",
    messages=[{"role": "user", "content": "你好"}],
)
print(resp.choices[0].message.content)
```

## system / developer 消息

muse.ai 的 `chat.stream` 没有独立的 `system` 角色。网关用两层机制让 system 语义真正生效（0.7.0）：

1. **VM 托管规则**：账号 VM 的 `IDENTITY.md` / `SOUL.md` / `AGENTS.md` 是 Agent 每次会话开始都会读取的自述文件。网关在里面维护一段带标记的托管规则：API 会话跟随 system 指令、禁用账号记忆工具（`muse.memory_search` / `muse.memory_get` / MEMORY.md）、优先使用已注册客户端命令。原文件先备份到 `data/harness_backup/`，`POST /admin/harness/{id}/restore` 可随时还原，`MUSE2API_NATIVE_HARNESS=false` 可整体关闭。
2. **会话内指令块**：`system` / `developer` 消息收集成前置指令块，当前用户消息单独放最后；后续轮次只发送新的用户轮。不同系统提示词生成不同指纹，驱动据此决定是否新开线程。

实测：带 system 的会话里，模型会自称 system 定义的姓名、精确跟随输出格式、不泄露账号主人的记忆。没有 system 消息、只用普通用户文本描述身份时，直接问"你是谁"仍可能自称 Muse——这与 OpenAI 的语义一致（user 弱于 system）。

## 工具调用

请求里带 `tools` 时，网关有两种路径：

| `MUSE2API_TOOL_CHANNEL` | 行为 |
|---|---|
| `auto`（默认） | 优先把工具注册成 muse.ai 原生客户端命令，Planner 兜底 |
| `native` | 只用原生客户端命令 |
| `planner` | 只用扁平 JSON Planner |

两条路径最终都返回标准 OpenAI `tool_calls`：

```json
{
  "id": "call_...",
  "type": "function",
  "function": {
    "name": "get_weather",
    "arguments": "{\"city\":\"Shanghai\"}"
  }
}
```

客户端把工具结果以 `role: "tool"` 发回来后，网关续接同一线程继续回答。流式请求返回 `delta.tool_calls`，`finish_reason` 为 `tool_calls`。

## 参数支持（0.10.0）

| 参数 | 行为 |
|---|---|
| `response_format: {"type": "json_object"}` | 注入 JSON-only 指令块；产出剥掉代码围栏并用 `json.loads` 校验，不合格自动换线程重试（`MUSE2API_TOOL_RETRIES` 次），仍失败时返回原始文本 |
| `response_format: {"type": "json_schema", ...}` | 同上，schema 摘要一并写进指令块 |
| `stop`（字符串或列表，最多生效于纯文本路径） | 命中即在该序列前截断并结束流式；上游轮次同时被取消（不走完整个生成） |
| `n > 1` | 非流式：并发独立补全，合并成多个 `choices`（临时线程，不共享会话）；流式：按到达序交错输出带 `index` 的 chunk（与 api.openai.com 一致），`stream_options.include_usage` 会追加 usage chunk |
| `max_tokens` / `max_output_tokens` | 按 token 近似值（约 4 字符/token）截断输出，`finish_reason: "length"`（Responses：`status: "incomplete"`）——muse.ai 没有原生采样控制，截断是唯一能兑现上限的方式 |
| `stream_options` | 仅在 `stream: true` 时有效；不带 stream 传会得到 400（与 OpenAI 一致） |
| `temperature` / `top_p` / `presence_penalty` 等 | 接受但忽略——muse.ai 不暴露采样控制 |
| 未知 `model` | 404 `model_not_found`（`param: "model"`）；只接受 `muse-chat` / `muse-image` / `muse-video`，不识别任何第三方模型名 |

## 错误与响应头（0.10.0）

所有失败路径——路由不存在（404）、方法不对（405）、参数校验、鉴权、上游故障——都返回
OpenAI 错误信封 `{"error": {"message", "type", "param", "code"}}`；每个响应都带
`x-request-id`、`openai-version: 2020-10-01` 和 `openai-processing-ms`，SDK 的重试日志
和错误解析与 api.openai.com 拿到的形状一致。

## 文件与音频输入（0.10.0）

chat completions 与 Responses 都接受附件，上传为 muse.ai 的 `file_ref` 附件——
逆向实测（probe 17）Agent 会真的读取文件内容，并声称可用音频转录工具处理音频：

```python
resp = client.chat.completions.create(
    model="muse-chat",
    messages=[{"role": "user", "content": [
        {"type": "text", "text": "附件里约定的暗号是什么？"},
        {"type": "file", "file": {
            "file_data": "data:text/plain;base64,..." ,  # 也支持 PDF 等任意 MIME
            "filename": "notes.txt"}},
    ]},
])
# Responses：input: [{"type": "input_file", "file_data": "data:...", "filename": "..."}]
```

音频用 `{"type": "input_audio", "input_audio": {"data": <base64>, "format": "wav"}}`。
`file_id`（OpenAI 托管文件）不支持——请内联 `file_data`。

## Responses API（0.10.0）

`/v1/responses` 不再只是转发：

- **有状态续聊**：响应默认存储（`data/responses.json`，TTL `MUSE2API_RESPONSE_TTL` 默认 7 天），
  下一次请求带 `previous_response_id` 即自动续上整段对话；`store: false` 不落存储；
  未知 id 返回 404（`param: "previous_response_id"`）。
- **函数调用**：请求带 `tools`/`tool_choice` 时走与 chat completions 相同的规划器，
  输出标准 `function_call` 项；`function_call` / `function_call_output` 可作为输入项回传续接；
  流式有 `response.function_call_arguments.delta/done` 事件。
- **完整事件序**：`output_text.done` / `content_part.done` 补齐，`max_output_tokens`
  触发 `status: "incomplete"` + `response.incomplete` 事件。
- `instructions` 走与 chat completions 相同的 system 注入链（托管规则 + 指令块）。

## 浏览器直连

`/v1/*` 默认返回 `Access-Control-Allow-Origin: *`（与 api.openai.com 一致），浏览器端应用（Open WebUI、LobeChat web、LibreChat 等）可以直接把 base_url 指过来。需要收紧时设置 `MUSE2API_CORS_ORIGINS=逗号分隔的来源列表`。`/admin/*` 仍然只对 Cookie 导入插件（`chrome-extension://`）放行跨域。

## 多轮对话

与 OpenAI 一样，补全是无状态的：模型能看到的上下文就是请求 `messages` 里的内容。`user` 字段只影响网关的线程复用，不引入服务端记忆。

- 默认无状态：每个请求新开线程并发送完整历史，客户端之间不会串上下文。
- 传稳定的 `user` 字段、且请求携带完整历史时，同一段对话会续接同一个线程，网关只把新增的用户消息发给上游（省 token、降首字延迟）。
- 只发送最新一条消息、不带历史时，行为与 OpenAI 完全一致：模型没有先前轮次的上下文（`user` 字段不是记忆）。
- 会话键按 API Key 隔离；两把 Key 传相同 `user` 也不会共享线程。
- 没有 `user` 的匿名请求永远不会续接别人的线程。
- 同一会话的并行请求会在同一线程上排队执行（0.8.0 起），不会互相破坏对方的回复流。
- 客户端断开、总超时或换号重试会取消上游正在执行的轮次并丢弃该线程（0.9.0 起）；下一次请求自动重发完整历史，不会串进半截回复。

## 已知限制

- muse.ai 是 Agent，不是可完全控制的 LLM；它可能拒绝某些请求，或给出与普通 LLM 不同的结果。
- 工具调用优先走原生通道，Planner 是兜底；连续拒绝、重试耗尽后可能返回普通文本而不是 `tool_calls`。
- 账号级记忆属于 muse.ai 账号；网关通过隔离前导 + 托管规则在 API 会话中禁用它（0.7.0 实测有效），但无法删除上游账号里的记忆文件本身。
- 只有 `ui.navigate.*` / `ui.set.*` 命名空间的客户端命令会被 muse.ai 路由（逆向确认）。
- 历史图片只保留占位符，跨线程依赖完整图片细节时可能丢失。
- 单账号默认放开到 12 并发（上游逆向实测）；更高并发请增加账号，而不是继续调大这个值。
