# OpenAI 客户端接入

muse2api 对外只说 OpenAI 协议。任何 OpenAI SDK、聊天客户端或 Agent 框架，把 `base_url` 指过来就能用；网关不会识别某个特定平台，也没有平台专用的特殊分支。

## 基本配置

| 字段 | 值 |
|---|---|
| API Base | `http://<host>:18610/v1` |
| API Key | `MUSE2API_API_KEY`，或控制台创建的 `m2a-...` 密钥 |
| 模型 | `muse-chat`，别名 `gpt-4o` / `dall-e-3` |
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

muse.ai 的 `chat.stream` 没有独立的 `system` 角色。网关会把 `system` / `developer` 消息收集成一个前置指令块，再发送当前用户消息；后续轮次只发送新的用户轮。不同系统提示词会生成不同指纹，驱动据此决定是否新开线程。

网关不做以下事情：

- 不修改 muse.ai 账号里的 IDENTITY / SOUL / USER / AGENTS / MEMORY 文件；
- 不修改回复内容，也不做平台特定的重试；
- 不注入任何平台特定的场景 XML 或会话头。

系统提示词只是被转换成模型更容易遵循的指令块，模型仍可能弱化其中的部分约束。

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

## 多轮对话

- 默认无状态：每个请求新开线程并发送完整历史，客户端之间不会串上下文。
- 传稳定的 `user` 字段后，同一段对话会续接同一个线程，后续只发送新的用户消息。
- 会话键按 API Key 隔离；两把 Key 传相同 `user` 也不会共享线程。
- 没有 `user` 的匿名请求永远不会续接别人的线程。

## 已知限制

- muse.ai 是 Agent，不是可完全控制的 LLM；它可能拒绝某些请求，或给出与普通 LLM 不同的结果。
- 工具调用优先走原生通道，Planner 是兜底；连续拒绝、重试耗尽后可能返回普通文本而不是 `tool_calls`。
- 账号级记忆属于 muse.ai 账号；网关按 API Key + `user` 隔离线程，但无法删除上游账号里的记忆。
- 历史图片只保留占位符，跨线程依赖完整图片细节时可能丢失。
