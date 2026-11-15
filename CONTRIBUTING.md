# 参与贡献

## 开发环境

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e '.[dev]'
pytest && ruff check .
```

默认使用 `MUSE2API_DRIVER=mock`，不需要真实账号，也不访问网络，可以安心开发 API 层和前端。

## 认领任务

待办任务列在 [TODO.md](TODO.md)。动手前请先开一个标题为 `[Claim] <ID> <任务名>` 的 issue，并把 GitHub ID 填到 TODO.md 的 Owner 列，避免重复劳动。代码里标了 `TODO(contributors)` 的地方是预留扩展点。

## 约定

- 依赖方向：`api → services → accounts/core → drivers → upstream`，禁止反向引用。
- 依赖 muse.ai 页面结构的代码只能放在 `drivers/browser/dom.py`；上游地址和 Cookie 名只能放在 `upstream/muse.py`。
- 新增驱动能力时，同步更新 `DriverCapabilities`，并在 `MockDriver` 里补一个假实现，保证测试永远不需要网络。
- 上游错误必须映射到 `errors.py` 里的类型；驱动内部不要吞异常。
- 测试不得访问真实 muse.ai；需要上游数据时使用录制的 fixture。
- 永远不要提交 Cookie、`data/` 目录或 `.env`。

## 新增驱动

1. 在 `src/muse2api/drivers/<name>/` 下实现 `MuseDriver` 子类。
2. 在 `drivers/registry.py` 注册，并把名字加入 `config.DriverName`。
3. 至少实现 `chat_stream`；其他能力在完成前抛出 `FeatureNotImplemented`。
4. 补充测试和文档。

## 提交变更

- 分支命名：`feat/<模块>`、`fix/<issue>`。
- 提交 PR 前确保 `pytest` 和 `ruff check .` 通过。
