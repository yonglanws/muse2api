# AstrBot 适配测试报告

- 日期：2026-11-16（0.5.1 更新）
- 版本：muse2api 0.5.1（原生协议驱动 + 原生客户端工具通道 + AstrBot 适配层）
- 本地环境：Windows，AstrBot Desktop 4.29.4（`D:\AstrBot`），Python 3.12
- 服务器环境：192.168.31.54，Docker，`muse2api-web:protocol-0.5.1`（原浏览器镜像 `muse2api-web:astrbot-0.4.1` 保留回滚）
- 账号：Muse 账号「慵懒午睡」（唯一账号）
- 上游：muse.ai（Meta 的个人 AI Agent，不是普通 LLM）

> 测试容器与生产容器使用同一个 Muse 账号。为避免两个浏览器争用同一会话，测试完成后测试容器已停止；生产容器保留运行。
> 本地 AstrBot 测试使用 AstrBot 自带的 Provider 代码（`ProviderOpenAIOfficial`）和服务器数据库副本中的真实 Persona，不涉及生产 AstrBot。

## 1. 结论

| 目标 | 结果 | 说明 |
|---|---|---|
| 系统提示词 / 角色支持 | ✅ 通过 | Persona 被放到当前用户轮，角色语气、身份、昵称均生效；出戏免责声明被清洗 |
| 用户之间上下文不串 | ✅ 通过 | 并发两个会话各自保存/取回不同暗号，没有串扰；无 conversation id 的请求始终新线程 |
| Agent / 工具调用 | ✅ 通过 | 扁平 JSON Planner → 标准 OpenAI `tool_calls`；工具结果续接、流式工具调用均验证 |
| AstrBot（QQ）亲和性 | ✅ 通过 | `x-astrbot-conversation-id`、昵称替换、热线程复用、Persona、工具循环、SSE 均验证 |
| 会话续期（keepalive） | ✅ 通过 | 修复 `httpx` 不走代理导致的 ConnectError；`/admin/accounts/{id}/renew` 返回 VM `RUNNING` |
| 回归测试 | ✅ 通过 | `pytest` 219 passed；`ruff check src tests` 通过 |
| 普通 OpenAI 客户端 | ✅ 通过 | 不带 AstrBot 头的非流式/流式请求行为不变；带 system 消息时自动走 Persona bridge |
| 纯协议驱动（Noise） | ✅ 通过 | 逐字流、线程隔离/续接、图片/视频、工具调用均实测 |

## 1.1 0.5.1 新增验证（2026-11-16）

逆向并接入原生客户端工具通道后，全部在生产容器（`muse2api-web:protocol-0.5.1`，18610）复测：

| 项目 | 结果 |
|---|---|
| 非流式原生 tool_calls | ✅ 3/3，`finish=tool_calls`，参数 `{"group_id":"123456","user_id":"789"}`（3.9–7.3s） |
| 流式原生 tool_calls | ✅ 3/3，`delta.tool_calls` + `finish_reason=tool_calls` |
| 工具结果续接 | ✅ 3/3，正确引用工具返回值（2.3–4.4s） |
| Persona 回归 | ✅ 3/3，角色身份稳定 |
| 会话隔离 A/B | ✅ 会话 B 对会话 A 的暗号明确回答“不知道” |
| 真实 AstrBot Provider（0.5.1 生产） | ✅ persona/tool/continuation/stream 全部通过 |
| 协议逆向验证 | ✅ `hidden_prompt` 不能覆盖身份；Shared Agents `/shared-agents/capabilities` 服务端 404（已下线）；`GET /model` 可用（当前账号仅 `auto/auto`）；`/MEMORY.md` 为账号记忆文件且每轮会话开头被读取——记忆只被隔离、从不写入 |

## 2. 复现步骤

### 2.1 本地 AstrBot Provider 测试

使用本机 AstrBot 4.29.4 的实际 Provider 代码，配置指向测试容器，并加载服务器数据库副本中的真实 Persona（晓山瑞希，8614 字）与两个 QQ 工具：

```python
ProviderOpenAIOfficial(
    {"id": "muse-test", "type": "openai_chat_completion", "provider": "openai",
     "key": ["<key>"], "api_base": "http://192.168.31.54:18611/v1",
     "model": "gpt-4o", "timeout": 240},
    {"identifier": False},
)
```

调用 `text_chat` / `text_chat_stream`，`system_prompt` 使用真实 Persona，`conversation_id` 使用固定值，`func_tool` 使用 AstrBot `ToolSet`。

结果：

```
[greet]     role=assistant  text='呀吼——早上好呀奶龙，今天也要元气满满一起加油哦♪'
            Muse leaked: False | nickname: True
[identity]  role=assistant  text='我是晓山瑞希，25点，Nightcord见。的MV师Amia哦～♪'
[tool]      role=tool       names=['get_group_member_info']
            args=[{'group_id': '123456', 'user_id': '789'}]
[tool-result] role=assistant text='呀吼——！查到啦♪ 群 123456 里成员 789 就是奶龙……还是管理员呢——！'
[stream]    chunks=4        text='……为什么螃蟹出门一直横着走？因为它太有“钳”途啦……'
            Muse leaked: False
[stream-tool] role=tool     names=['get_group_member_info']
```

这一步覆盖了 AstrBot 的 Provider 请求构造、`system_prompt`、`tools`、`tool_choice`、工具结果消息、流式累积和 `LLMResponse` 解析。

### 2.1.1 运行中的本机 AstrBot 注册与自检

本机正在运行的 AstrBot 4.29.4（`127.0.0.1:6185`）已通过 AstrBot 的 Provider API 注册：

- Provider Source：`muse2api` → `http://192.168.31.54:18610/v1`
- Provider：`muse2api/gpt-4o`（model `gpt-4o`，modalities: text/image/tool_use）
- AstrBot 的 `/api/v1/providers/test` 返回：

```json
{"id":"muse2api/gpt-4o","model":"gpt-4o","type":"chat_completion",
 "status":"available","error":null}
```

没有切换本机默认模型（仍是原来的 `午睡/deepseek-v4.1-flash`），避免影响本机其它测试；在 AstrBot 的「服务提供商」里选择 `muse2api/gpt-4o` 即可使用。配置备份为 `cmd_config.json.bak-muse2api-*`。

### 2.2 服务器测试容器集成测试

测试容器 `muse2api-astrbot-test`（端口 18611，隔离数据目录，`WARM_THREADS=2`）通过 OpenAI SDK 发送与 AstrBot 相同结构的请求（含 `x-astrbot-conversation-id` 和 `<conversation_scene>` XML）：

| 用例 | 结果 |
|---|---|
| 角色问候 | 通过；回复含当前昵称「奶龙」，不含 Muse/Meta |
| 身份问题 | 通过；返回「我是晓山瑞希～ 很高兴认识你呀～」 |
| 专用会话复用 | 通过；第二轮 `reusing hot page`，约 12–20 秒返回暗号 |
| 并发上下文隔离 | 通过；会话 1 取回「红色狐狸」，会话 2 取回「绿色乌龟」，互不包含 |
| 工具查询 | 通过；`finish_reason=tool_calls`，参数 `{"group_id":"123456","user_id":"789"}` |
| 工具结果续接 | 通过；在角色语气中返回「奶龙 / 管理员」 |
| 流式文本 | 通过；SSE 内容已清洗，无 Muse |
| 流式工具调用 | 通过；`delta.tool_calls` 中解析出 `send_group_message` |
| 同一 conversation id 切换 Persona | 通过；切换为宵崎奏后返回宵崎奏，不继承瑞希上下文 |
| 工具拒绝重试 | 通过；第一次拒绝后在新线程重试，返回 `tool_calls` |

### 2.3 生产冒烟测试

部署 `muse2api-web:astrbot-0.4.1` 后，在 18610 端口重复关键用例：

```
greet        19.6s  呀吼——！奶龙你好呀～♪ 我刚剪完一批素材正闲着呢……
identity     19.7s  我是晓山瑞希～ 很高兴认识你呀～
tool          8.0s  finish=tool_calls  get_group_member_info(...)
tool-result  11.5s  呀吼——！查到啦♪ …奶龙…管理员哦——！
stream       32.2s  夸当然可以：奶龙你超有活力、又很会活跃气氛……
```

会话续期：

```
POST /admin/accounts/acc_.../renew
{"ok":true,"meta":{"session_status":"assigned","vm_state":"RUNNING",
                   "vm_state_after":"RUNNING","vm_wake_ok":true}}
```

### 2.4 纯协议驱动实测（0.5.0）

隔离测试容器 `muse2api-protocol-test`（18611，`MUSE2API_DRIVER=protocol`，镜像 `muse2api-web:protocol-0.5.1`）：

| 用例 | 结果 |
|---|---|
| 首轮聊天 | 200，16.4 秒，回复 `PROTO-OK` |
| 同会话第二轮 | 200，10.1 秒，正确复述 `BLUE-42`；只发送新用户消息 |
| 并发两会话隔离 | `PBLUE-31` / `PGREEN-84` 各取各的暗号，互不串扰 |
| 通用 system 提示词（猫） | 3/3 返回 `喵`；不再自称 Muse |
| 通用 system 提示词（海盗船长） | 3/3 以船长身份回答 |
| AstrBot Persona + 工具 | 200，6.1 秒，`get_group_member_info`；参数归一化为字符串 |
| 工具结果续接 | 200，3.4 秒，角色语气返回“奶龙 / 管理员” |
| 流式工具调用 | 4.9 秒，标准 `delta.tool_calls` + `finish_reason=tool_calls` |
| 通用流式聊天 | 首字 15.4 秒，12 个真实 `delta.content`，17.0 秒完成 |
| 图片生成 | 200，24.5 秒，返回 583 KB WebP |
| 视频生成 | 200，约 52 秒，1280×720 MP4 |
| AstrBot 记忆隔离 | A 会话记住 `ISO-xxxx`，B 会话只回复“不知道” |
| 连接复用 | 同一账号连续请求只建立一次 Noise 连接 |

协议栈细节：`chat.stream` + 线程级 `chat.subscribe`（带 `session_id`）才能收到线程事件；不带 `session_id` 会落进账号当前主线程，因此不用于匿名/多用户请求。`hidden_prompt` 实测不能覆盖身份，已不使用。

### 2.5 本机 AstrBot Provider 直连生产 0.5.0

用本机 AstrBot 4.29.4 的实际 `ProviderOpenAIOfficial` 指向生产 `http://192.168.31.54:18610/v1`，不经过 AstrBot WebChat/生产 AstrBot：

```
text_chat(persona)        -> '我是晓山瑞希，25点的MV担当Amia哦～'
text_chat(ToolSet)        -> names=['get_group_member_info']
                             args=[{'group_id': '123456', 'user_id': '789'}]
text_chat_stream          -> deltas == final，逐字内容一致
```

这覆盖了 AstrBot 的 Provider 请求构造、`system_prompt`、`tools`、`conversation_id` 请求头、流式累积和 `LLMResponse` 解析。

### 2.6 普通客户端回归

不带 AstrBot 头的请求：

```
generic non-stream 10.8s: 1+1等于2。
generic stream     13.5s: 晚安，小红，好梦。
```

行为与旧版一致（非 AstrBot 请求不做昵称替换，账号主人仍可能被称为「小红」）。

## 3. 发现的问题与修复

| 问题 | 现象 | 修复 |
|---|---|---|
| system prompt 不生效 | `[System]` 文本被当作对话内容；无法改名/改语气 | Persona 放入当前用户轮，使用「角色设定｜最高优先级」+ 输出规则 |
| 出戏免责声明 | 「可以陪你用那种语气……不过我还是 Muse」 | `sanitize_reply` 按句清洗；保留正文，删掉免责声明 |
| 身份问题固定回答 | 「我是 Muse，Meta 打造的……」 | 检测身份问题并用 Persona 名称生成一句话介绍 |
| 安全拒绝外泄 | 「我不能扮演成未成年人……」直接发给 QQ | 风格兜底重试；仍拒绝时替换为角色化安全回复 |
| 会话头被忽略 | AstrBot 的 `x-astrbot-conversation-id` 没用于续接 | `conversation_key` 优先使用该头；按 API key 隔离 |
| 系统时间导致断线程 | 每轮 system prompt 都变，旧逻辑判定历史不匹配 | 续接比较忽略 system/tool，角色指纹忽略动态时间 |
| tools 被忽略 | AstrBot 的 Agent 调用永远拿不到 `tool_calls` | 扁平 JSON Planner + `parse_tool_plan` + `make_tool_call`；工具结果续接 |
| 工具拒绝 | 「我没有接入 QQ / 没有这个工具」 | 严格 Planner 重试（默认 2 次，新线程）；流式同样支持 |
| keepalive 失败 | `httpx.ConnectError`，会话到期 | `MUSE2API_UPSTREAM_PROXY` 传给 `muse.renew_session`，默认回退到 `BROWSER_PROXY` |
| 多会话 tab 淘汰 | 新会话总淘汰最早创建的 tab | 改为 LRU，优先保留最近使用的会话 |
| 空/碎片回复 | 清洗后只剩「我一句话说清：。」 | 退化检测 + 角色兜底 / 通用「我在的～」 |

## 4. 已知限制

1. muse.ai 的身份和安全策略是模型行为，无法 100% 覆盖。它仍可能：
   - 拒绝扮演年龄较小的虚构角色；
   - 拒绝把「暗号」写入长期记忆；
   - 用「我不会真的冒充她」开头。适配层会清洗/重试，但极端情况下只能返回安全兜底。
2. 工具 Planner 是概率性的。默认 2 次重试后若仍失败，AstrBot 收到的是普通文本而不是 `tool_calls`。
3. 账号主人自己的记忆（例如「名字是小红」）仍在 Muse 账号级；适配层只替换昵称，不清除记忆。
4. 历史图片只保留占位符，不重复上传；跨线程依赖完整历史时，图片细节可能丢失。
5. 非 AstrBot 客户端不做昵称替换/免责声明清洗，仍可能出现账号主人的称呼。

## 5. 变更文件

- `src/muse2api/core/astrbot.py`（新增）：AstrBot 检测、Prompt/Planner、清洗、工具解析、重试辅助；工具参数按 schema 归一化。
- `src/muse2api/drivers/protocol/`（新增）：Noise 握手、protobuf、`chat.stream`/订阅、逐字流、线程、图片/视频。
- `src/muse2api/api/routes/chat.py`：AstrBot 分支、工具调用、缓冲流式、重试；带 system 的普通请求走 Persona bridge。
- `src/muse2api/api/deps.py`：`x-astrbot-conversation-id` 会话键。
- `src/muse2api/core/prompt.py`：续接比较忽略 system/tool，新增 `same_history_prefix`。
- `src/muse2api/drivers/base.py`：`followup_prompt`、`tool_continuation`、`persona_fingerprint`、`page_echo`、`force_new_thread`。
- `src/muse2api/drivers/browser/driver.py`：热线程续接、Persona 指纹、LRU tab、page_echo。
- `src/muse2api/upstream/muse.py` / `drivers/http/driver.py`：上游代理。
- `src/muse2api/config.py`：AstrBot / 工具 / 代理 / 协议线程配置。
- `tests/test_astrbot_compat.py`（新增）：30 个 AstrBot 适配测试。
- `docs/ASTRBOT.md`（新增）：接入与配置说明。

## 6. 部署状态

| 容器 | 镜像 | 状态 |
|---|---|---|
| `muse2api-web` | `muse2api-web:protocol-0.5.0` | 运行中，18610（`MUSE2API_DRIVER=protocol`） |
| `muse2api-web-0.4.1` | 原 `muse2api-web:astrbot-0.4.1` | 已停止，保留回滚 |
| `muse2api-astrbot-test` | `muse2api-web:astrbot-test` | 已停止，隔离数据目录保留 |

回滚命令见 `docs/ASTRBOT.md` 第 7 节。
