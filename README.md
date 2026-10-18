# Mailuo (脉络) — 面向具体内容与核心主题的 Agent Session 管理系统

> 解决跟 Agent 对话时“单窗口穿插杂事导致上下文混乱”与“多窗口碎片化找不到历史讨论”的核心痛点。

## 核心特性

1. **围绕具体内容的独立 Session (`TopicSession`)**
   - 每个具体讨论话题拥有独立的结构化状态摘要（`summary`、已达成的关键结论 `key_decisions`、待解决问题 `open_questions`）。
2. **穿插任务自动隔离 (`Side-Tangent` 防污染)**
   - 在深入探讨某个具体话题时，如果临时穿插其他杂项问题（如“顺便问一下正则怎么写”），路由器自动将其标记为 `is_ephemeral=True` 并在隔离沙箱中回答，**绝不污染当前主话题的上下文与长期摘要**。
3. **自然语言随时召回历史话题 (`Session Recall`)**
   - 直接对 Agent 说：*“把上次我们讨论的那个 XX 话题的 session 调出来”* 或 *“上次讨论的另外一个关于 YY 的话题能调出来吗”*，系统通过混合检索（BM25 词法 + 向量余弦相似度）精准定位并无缝恢复当时的结构化上下文。
4. **自主归纳整理『核心主题』 (`Core Themes Consolidation`)**
   - 直接对 Agent 说：*“把这些 session 整理成跟我有关的最核心主题”*，系统会自动把零散的话题聚类合并为高维度的 `CoreTheme`，提炼跨会话的核心共识。

## 快速开始

### 1. 运行全流程自动化演示 (无需配置 API Key 即可体验完整脉络流转)

```bash
PYTHONPATH=src python3 -m mailuo.cli --demo
```

### 2. 进入交互式命令行 (REPL)

```bash
PYTHONPATH=src python3 -m mailuo.cli
```

如果希望接入在线大模型（OpenAI / DeepSeek / 本地 Ollama 等兼容接口），只需设置环境变量：

```bash
export MAILUO_API_KEY="your-api-key"
export MAILUO_BASE_URL="https://api.openai.com/v1"  # 可选
export MAILUO_MODEL="gpt-4o-mini"                   # 可选
PYTHONPATH=src python3 -m mailuo.cli
```

### 3. 运行单元测试

```bash
PYTHONPATH=src python3 -m pytest -v
```