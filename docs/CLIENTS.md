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

## 多轮对话

- 默认无状态：每个请求新开线程并发送完整历史，客户端之间不会串上下文。
- 传稳定的 `user` 字段后，同一段对话会续接同一个线程，后续只发送新的用户消息。
- 会话键按 API Key 隔离；两把 Key 传相同 `user` 也不会共享线程。
- 没有 `user` 的匿名请求永远不会续接别人的线程。

## 已知限制

- muse.ai 是 Agent，不是可完全控制的 LLM；它可能拒绝某些请求，或给出与普通 LLM 不同的结果。
- 工具调用优先走原生通道，Planner 是兜底；连续拒绝、重试耗尽后可能返回普通文本而不是 `tool_calls`。
- 账号级记忆属于 muse.ai 账号；网关通过隔离前导 + 托管规则在 API 会话中禁用它（0.7.0 实测有效），但无法删除上游账号里的记忆文件本身。
- 只有 `ui.navigate.*` / `ui.set.*` 命名空间的客户端命令会被 muse.ai 路由（逆向确认）。
- 历史图片只保留占位符，跨线程依赖完整图片细节时可能丢失。
