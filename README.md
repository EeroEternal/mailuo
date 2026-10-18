# Mailuo (脉络) — 跨 `pi.dev` 与 `Google Antigravity (agy)` 的话题 Session 与核心主题管理系统

> 解决跟多个 Agent 对话时“单窗口穿插杂事导致上下文混乱”、“多窗口/跨工具碎片化找不到历史讨论”的核心痛点。

## 核心特性

1. **直接拉起并托管 `pi.dev` (`pi`) 与 `Google Antigravity` (`agy`)**
   - **专属话题会话绑定**：每个具体话题 (`TopicSession`) 自动分配并记住专属的 `pi --session-id <uuid>` 和 `agy --conversation <id>`。
   - **穿插提问物理级防污染 (`Side-Tangent`)**：当你在某个话题下临时穿插无关问题（如“顺便问一下正则怎么写”）时，Mailuo 拉起 `pi` 会自动附加 `--no-session` 参数，确保 `pi` 原生会话文件绝不被杂事污染！
   - **跨 Agent 记忆共享**：无论你上一轮是用 `pi` 还是 `agy` 聊的，Mailuo 都会把提炼出的结构化脉络摘要通过 `--append-system-prompt` / 上下文注入同步给下一个被拉起的 Agent。
2. **一键同步本机已有的 `pi` 与 `agy` 历史记录 (`--sync`)**
   - 自动扫描并解析 `~/.pi/agent/sessions/**/*.jsonl` 与 `~/.gemini/antigravity-cli/brain/*/.system_generated/logs/transcript.jsonl`，将你过去在 `pi` 和 `agy` 里聊过的所有具体内容自动纳入脉络管理。
3. **自然语言随时召回历史话题 (`Session Recall`)**
   - 直接说：*“把上次我们在 pi 里讨论的那个 SmartGate 推理引擎的话题调出来”*，系统立即检索定位并恢复该话题上下文（包括对应的 `pi` session ID 和 `agy` conversation ID）。
4. **跨 Agent 自主归纳整理『核心主题』 (`Core Themes Consolidation`)**
   - 直接说：*“把这些 session 整理成跟我有关的最核心主题”*，自动将散落在 `pi`、`agy` 和本地的零散话题聚类合并为高维度的 `CoreTheme`。

## 快速使用

### 1. 运行跨 Agent 全流程演示（含扫描导入本机真实的 `pi` 与 `agy` 会话）

```bash
PYTHONPATH=src python3 -m mailuo.cli --demo
```

### 2. 一键导入本机已有的 `pi.dev` 和 `Google agy` 历史会话

```bash
PYTHONPATH=src python3 -m mailuo.cli --sync all
```

### 3. 在当前话题脉络下直接拉起 `pi` 或 `agy`

```bash
# 使用 pi.dev 提问并按脉络自动记录
PYTHONPATH=src python3 -m mailuo.cli --agent pi --ask "我们来讨论一下网关层的上下文分片缓存设计"

# 在当前激活的具体话题下，直接拉起 pi.dev 的原生交互式 TUI（退出后自动同步记录回 Mailuo）
PYTHONPATH=src python3 -m mailuo.cli --spawn pi

# 或者直接拉起 Google Antigravity (agy) 交互式会话
PYTHONPATH=src python3 -m mailuo.cli --spawn agy
```

### 4. 启动交互式脉络控制台 (REPL)

```bash
PYTHONPATH=src python3 -m mailuo.cli
```
在控制台内支持快捷指令：
- `/pi <内容>`：调用 `pi.dev` 处理当前话题
- `/pi-tui`：直接拉起 `pi` 的交互式 TUI（自动带上当前话题的 `--session-id` 和脉络摘要）
- `/agy <内容>`：调用 Google `agy` 处理当前话题
- `/agy-tui`：直接拉起 `agy` 的交互式界面
- `/sync`：从磁盘增量同步 `pi` 和 `agy` 的历史记录
- `/themes`：把所有跨 Agent 的话题整理为你的“最核心主题”
- `/btw <内容>`：强制以临时隔离模式提问（对 `pi` 自动启用 `--no-session`）