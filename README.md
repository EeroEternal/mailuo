# Mailuo (脉络 `mai`) — 支持多用户注册隔离、跨机器同步与多 Agent (`pi.dev` / `Google agy`) 的脉络管理系统

> 解决跟多个 Agent 对话时“单窗口穿插杂事导致上下文混乱”、“多窗口/跨机器碎片化找不到历史讨论”的核心痛点。

## 1. 任意机器一键安装 (`pip install`)

在其他任何机器的 Python 环境中，直接通过 GitHub 仓库一键安装：

```bash
pip install git+https://github.com/EeroEternal/mailuo.git
# 或者克隆仓库后本地安装:
# pip install .
```

*(无需在其他机器上安装 Node.js / Wrangler 或配置 Cloudflare 密钥，客户端会自动连接已部署的 Cloudflare Worker 多租户网关 `https://mailuo-api.lipeng-sh.workers.dev` 并将数据加密隔离保存在 Cloudflare D1 `mailuo-db` 中。)*

---

## 2. 账号注册与多机器登录（每个人的数据严格隔离、绝不串号）

第一次在某台机器上使用 `mai` 时，会自动引导你注册或登录（也可以显式执行命令）：

```bash
# 1) 注册属于你自己的账号（自动登录并生成本地凭证 ~/.mailuo/auth.json）
mai register <用户名> <密码>
# 或者交互式输入密码（不回显）：
mai register

# 2) 在另一台电脑上登录同一个账号，立即无缝接续所有历史脉络
mai login <用户名> <密码>

# 3) 查看当前登录的是谁
mai whoami

# 4) 退出当前机器的账号
mai logout
```

---

## 3. 极简日常用法 (`mai`)

```bash
# 1) 直接对话（自动路由：延续话题 / 穿插提问隔离 / 调出旧话题 / 整理核心主题）
mai "我想讨论一下在 Cloudflare D1 上存储多 Agent 脉络的架构方案"

# 2) 临时穿插无关问题（自动进入隔离沙箱，绝不污染当前话题摘要）
mai "顺便问一下，帮我写个匹配邮箱的正则"

# 3) 自然语言调出历史话题 Session
mai "上次我们讨论的那个 Cloudflare D1 架构的话题调出来"

# 4) 在当前话题脉络下直接拉起 pi.dev 或 Google agy
mai pi                       # 直接进入 pi.dev 交互式 TUI（自动绑定当前话题的 --session-id，退出后自动同步记录到你的云端账号）
mai pi "帮我分析一下这段代码" # 用 pi.dev 单次回答并记录到当前话题
mai agy                      # 直接进入 Google Antigravity (agy) 交互式界面

# 5) 查看你账号下的所有具体话题与整理核心主题
mai ls                       # 查看当前账号下的所有 Topic Sessions
mai themes                   # 把当前账号下的零散 Session 自动整理成『最核心主题』
mai sync                     # 将当前机器上的 ~/.pi/agent/sessions 和 ~/.gemini/antigravity-cli/brain 同步至你的云端账号
mai update                   # 升级已安装的 mailuo（pipx 或 pip），无需手动重装

# 注：0.3.1 及更早版本没有 mai update，需先执行一次 `pipx upgrade mailuo`（或 `pip install -U mailuo`）

# 6) 进入交互式控制台
mai
```