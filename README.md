# Mailuo (脉络) — 基于 Cloudflare D1 的跨 Agent (`pi.dev` / `Google agy`) 脉络与核心主题管理

> 解决跟多个 Agent 对话时“单窗口穿插杂事导致上下文混乱”、“多窗口/跨工具碎片化找不到历史讨论”的核心痛点。所有数据默认实时同步保存在 **Cloudflare D1 (`mailuo-db`)**。

## 1. 安装 (`pip install`)

```bash
pip install -e .
# 或者直接通过 git 安装:
# pip install git+https://github.com/EeroEternal/mailuo.git
```

安装后即可在任意终端直接使用 `mailuo` 命令。

## 2. 极简 CLI 用法

```bash
# 1) 直接对话（自动路由：延续话题 / 穿插提问隔离 / 调出旧话题 / 整理核心主题）
mailuo "我想讨论一下在 Cloudflare D1 上存储多 Agent 脉络的架构方案"

# 2) 临时穿插无关问题（自动进入隔离沙箱，绝不污染当前话题摘要）
mailuo "顺便问一下，帮我写个匹配邮箱的正则"

# 3) 自然语言调出历史话题 Session
mailuo "上次我们讨论的那个 Cloudflare D1 架构的话题调出来"

# 4) 在当前话题脉络下直接拉起 pi.dev 或 Google agy
mailuo pi                       # 直接进入 pi.dev 交互式 TUI（自动绑定当前话题的 --session-id，退出后自动记录到 Cloudflare D1）
mailuo pi "帮我分析一下这段代码" # 用 pi.dev 单次回答并记录到当前话题
mailuo agy                      # 直接进入 Google Antigravity (agy) 交互式界面

# 5) 查看 Cloudflare D1 中的所有具体话题与整理核心主题
mailuo ls                       # 查看所有 Topic Sessions
mailuo themes                   # 把所有零散 Session 自动整理成与你相关的『最核心主题』
mailuo sync                     # 将本机 ~/.pi/agent/sessions 和 ~/.gemini/antigravity-cli/brain 同步至 Cloudflare D1

# 6) 进入交互式控制台
mailuo
```

## 3. Cloudflare D1 存储说明

- **数据库名称**：`mailuo-db`
- **Database ID**：`74d3d401-b084-41bd-9f13-0be9a62e56ea`
- **认证机制**：[`MailuoStorage`](file:///Users/xinference/github/mailuo/src/mailuo/storage.py) 会自动读取本机 `wrangler` 登录凭证（或环境变量 `CLOUDFLARE_API_TOKEN`），无需手动配置即可直连 Cloudflare D1；在离线环境下会自动回退至本地 `~/.mailuo/mailuo.db`。