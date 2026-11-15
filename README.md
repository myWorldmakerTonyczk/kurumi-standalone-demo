# 久留美 · AI 模拟交易与社区工作区

**Axiom 第一届黑客松参赛作品**

给一个有自己名字和人格的 AI 智能体一笔虚拟资金，观察它如何看行情、做决定、记录理由，再通过日志、日记和社区回看决策过程。

| 入口 | 链接与说明 |
| --- | --- |
| 阿里云体验地址 | [打开用户端](https://47.95.233.156:18443/) |
| 源码仓库 | [GitHub · kurumi-standalone-demo](https://github.com/myWorldmakerTonyczk/kurumi-standalone-demo) |
| 登录方式 | 展示账号 `show` · 密码 `12345678` |

**所有成交和盈亏都记录在模拟账本里，资金为虚拟资金。**

![交易工作台，包含报价、K 线、账户指标和虚拟下单区](docs/screenshots/trading-workspace.png)

## 要解决的问题

想观察 AI 怎样做交易决策，单轮问答很难复现当时的行情、持仓和此前做过的操作。只看最终盈亏，也很难追溯智能体为何下单、改变计划，或选择继续观望。这个项目把角色对话、工具操作、账本和复盘放在同一个工作区，让每一笔模拟操作都有上下文可查。

## 核心功能

| 模块 | 能看到什么 |
| --- | --- |
| 交易工作台 | 报价、K 线、模拟净值和持仓，支持虚拟开仓、平仓和设置保护线 |
| 智能体 | 自定义名字与人格，支持持续私聊、读取账本和定时看盘；接入模型后可调用工具 |
| 日志与复盘 | 并列显示智能体、用户和系统的操作，保存看盘摘要、计划、日记以及成交结果 |
| 社区 | 论坛搜索、标签、评论和群聊；支持把复盘整理成可编辑的社区内容 |
| 智能体财富榜 | 按总财富、周盈利、累计收益率比较模拟表现；周盈利按本周已平仓盈亏减去本周成交费计算 |
| 管理端 | 用户、工作区、行情与运行配置 |

## 90 秒体验路线

1. 使用上方展示账号登录，打开**交易工作台**，看当前报价、K 线和账户净值。
2. 打开与智能体的对话。在已配置模型的实例里，可以问「说说你现在怎么看这个账户，接下来打算做什么？」也可以点「让她看一次盘」。
3. 打开**交易日志**或**计划与经验**，查看做了什么、理由是什么。遇到休市时，用已有记录展示这条流程。
4. 打开**社区**看群聊和复盘分享，再点右侧的**财富榜**切换三种排名。本地离线模式可在没有模型 Key 的情况下体验基础界面和演示回复。

![统一交易日志，按时间显示看盘摘要、计划更新、观望和平仓](docs/screenshots/trading-journal.png)

## 更多界面截图

### 计划与经验

![计划与经验页面，左侧是当前交易计划，右侧是按日期查看的交易日记](docs/screenshots/plans-and-diary.png)

### 社区群聊

![社区群聊，展示成员与智能体的消息、表情包和提及](docs/screenshots/community-chat.png)

## 架构与技术选型

```mermaid
flowchart LR
    UI["浏览器工作区"] --> API["aiohttp 服务"]
    API --> Agent["LangChain 智能体与定时任务"]
    API --> Ledger["模拟账本与结算"]
    Agent --> Tools["查询、模拟交易及社区工具"]
    Agent --> Model["可配置的大模型接口"]
    Tools --> Ledger
    Quote["行情适配层"] --> Ledger
    Ledger --> DB[("SQLite")]
    API --> DB
```

- **Python + aiohttp** 提供账号、浏览器 API 和后台任务。**SQLite** 保存模拟账本、日志、社区和配置，部署时可以独立保留数据目录。
- **LangChain / LangChain OpenAI** 负责模型与工具调用。模型可以选择操作，成交价格、权限和结果由本地模拟引擎校验和记录。
- 前端使用原生 JavaScript、CSS、HTML，使用 **TradingView Lightweight Charts** 绘制行情，不需要前端构建步骤。

## 起点声明与赛期工作

项目起点是已有的 [AstrBot 久留美模拟器插件](https://github.com/myWorldmakerTonyczk/astrbot_plugin_kurumi_simulator)。虚拟账本、结算、行情接入和原面板风格有赛前基础，独立版沿用了这些实现。

赛期主要完成了面向浏览器的独立服务与登录工作区、平台和外部智能体模式、社区与智能体联动、图片工具、自定义名称与人格设置，以及三项模拟财富排名。独立版从 [初始提交](https://github.com/myWorldmakerTonyczk/kurumi-standalone-demo/commit/e84fa62) 开始的演进记录可在仓库查看。

## 团队分工与 AI 辅助

| 成员 | 分工 |
| --- | --- |
| 陈泽坤 | 后端开发 |
| 王元辰 | 前端开发 |
| 杨其融 | 前端开发 |

开发阶段使用 **Codex** 辅助实现界面与交互、修改 Python 服务代码、排查问题、编写和运行测试、整理部署与 README。运行时的大模型用于角色对话、工具选择、看盘摘要和日记。模拟成交与余额由账本程序计算。

## 第三方与素材说明

- 后端与模型接入使用 [aiohttp](https://github.com/aio-libs/aiohttp)、[LangChain](https://github.com/langchain-ai/langchain) 和 SQLite。图表使用 [TradingView Lightweight Charts](https://github.com/tradingview/lightweight-charts)，其许可证保存在 [仓库内](views/dashboard/vendor/LICENSE-lightweight-charts.txt)。
- 行情可使用项目已接入的 biquote / OANDA，日级参考汇率可使用 Frankfurter。实际数据取决于实例配置和行情服务。
- 久留美角色和部分界面所展示的角色素材源于《FX 战士久留美》，原作与动画相关权利归各自权利人。上方截图来自项目实际界面。 [动画官方网站](https://fxkurumi-info.com/)

## 快速启动

需要 **Python 3.10 或更新版本**，本机已使用 Python 3.12 验证。首次安装依赖需要联网。

### Windows PowerShell

```powershell
git clone https://github.com/myWorldmakerTonyczk/kurumi-standalone-demo.git
cd kurumi-standalone-demo
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:KURUMI_LIVE = "0"
.\.venv\Scripts\python.exe app.py
```

直接调用虚拟环境里的 Python，无需激活脚本。已下载仓库时，从仓库目录内的 `python -m venv .venv` 开始即可。

<details>
<summary>Linux / macOS</summary>

```bash
git clone https://github.com/myWorldmakerTonyczk/kurumi-standalone-demo.git
cd kurumi-standalone-demo
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
KURUMI_LIVE=0 .venv/bin/python app.py
```

</details>

启动后保持终端运行，用浏览器打开下列入口。按 `Ctrl+C` 停止服务。

| 入口 | 地址 | 用途 |
| --- | --- | --- |
| 管理员登录 | <http://127.0.0.1:8790/> | 初始化管理员、创建用户、管理实例 |
| 用户登录 | <http://127.0.0.1:8791/> | 用户账号登录 |
| 用户工作区 | <http://127.0.0.1:8791/workspace/> | 登录后使用首页、交易与社区 |
| 健康检查 | <http://127.0.0.1:8791/api/health> | 确认用户服务可达 |

### 第一次使用

1. 打开终端打印的“管理员首次设置”链接，或在管理员入口输入一次性 Token。设置管理员账号与密码，**管理员密码至少 12 个字符**。
2. 在管理端创建用户，保存用户登录凭据。管理员账号与用户账号分别用于两个入口。
3. 从用户入口登录。没有交易实例时，按引导创建角色与实例，初始虚拟资金为 **1,000 美元**；已有实例则直接进入工作区。
4. 首次体验可保持模型配置为空，在离线模式下聊天、查看模拟账本，并体验论坛、群聊和 AI 草稿。离线社区示例数据会按账号幂等初始化。
5. 需要 AI 自动看盘时，在个人设置中检查自动看盘开关、执行间隔和休息时段。

日常使用建议收藏 `/workspace/`：用户登录页根路径 `/` 会清除旧登录 Cookie。

## 行情模式与模型配置

### 先分清两个开关

`KURUMI_LIVE` 控制行情来源，个人设置中的模型与 Key 控制是否请求真实模型，两者独立。

| 启动方式 | 行情 | 角色聊天 / 自动看盘 |
| --- | --- | --- |
| `KURUMI_LIVE=0`，未配置模型与 Key | 固定 EUR/USD 演示报价 | 使用确定性演示回复与模拟操作 |
| `KURUMI_LIVE=0`，已配置模型与 Key | 固定 EUR/USD 演示报价 | 请求所配置的真实模型，可能产生 API 费用 |
| `KURUMI_LIVE=1`，或未设置此变量 | 真实行情源 | 需要配置模型与 Key，否则相关功能提示先配置 |

**运行 `python app.py` 默认使用真实行情。** 快速启动显式设置 `KURUMI_LIVE=0`，便于首次本地演示。离线行情只注入 EUR/USD 的固定买卖报价，不提供全品种历史行情。

行情任务每 5 秒刷新。离线且尚未保存个人偏好时，自动看盘间隔为 30 秒；保存后按个人设置执行。真实行情模式默认间隔为 1 小时，实际执行还受开关、实例状态与休息时段影响。

社区摘要和草稿包含确定性整理逻辑，不能把所有“AI 助手”输出都视为真实大模型生成。

### 接入模型

在用户个人设置中选择接口地址，填写服务商支持的模型名称与 API Key，再保存并发送一条消息验证。内置 OpenAI、DeepSeek、Gemini 接口预设；模型是否可用以服务商和账号权限为准。

- 默认模型接口为 `https://api.openai.com/v1`。
- API Key 留空会保留旧值；移除 Key 需使用界面的清除操作。
- 更换接口地址时，需要同时提供新 Key，或先清除旧 Key。
- 自定义 OpenAI 兼容接口需先加入 `KURUMI_MODEL_URLS`，再在个人设置中填写同一地址。允许列表按地址精确匹配，末尾 `/` 也要一致。
- 接口要求 HTTPS；本机回环地址可使用 HTTP，但同样需要加入允许列表。

例如，在启动服务前为本机模型服务加入允许地址：

```powershell
$env:KURUMI_MODEL_URLS = "http://127.0.0.1:8000/v1"
```

网页不会回显已保存的 Key，但 **Key 仍保存在 SQLite 中，未加密**。请保护数据目录和备份。

## 配置参考

配置通过启动进程的环境变量传入，项目不会自动读取 `.env` 文件。修改后重启服务。

| 变量 | 默认值 | 作用 |
| --- | --- | --- |
| `KURUMI_LIVE` | `1`（命令行启动） | `0` 为离线演示，`1` 为真实行情 |
| `KURUMI_DATA` | 仓库内 `data/` | 数据库、管理员配置和日志目录 |
| `KURUMI_MODEL_URLS` | 空 | 额外允许的模型接口地址，多个地址用英文逗号分隔 |
| `KURUMI_PUBLIC_URL` | `http://127.0.0.1:8791`，也可使用已保存的面板地址 | 用户面板公开地址；HTTPS 地址会启用用户 Cookie 的 Secure 属性 |

本机 HTTP 访问时保持本机地址。若提前设成 HTTPS 域名，本机 HTTP 登录可能因 Secure Cookie 无法保持会话。

## 项目结构

```text
kurumi-standalone-demo/
├── app.py                 # 独立服务入口、登录、模型调用与后台任务
├── dashboard_api.py       # 工作区接口与交易操作
├── config/                # 行情默认配置
├── src/
│   ├── core/              # 模拟账本、交易校验与结算
│   ├── market_data/       # 报价与行情数据
│   ├── agent/             # AI 工具、上下文处理与模型适配
│   ├── portal/            # 账号、访问控制、论坛与群聊
│   ├── users/             # 用户与实例管理
│   ├── diary/             # 交易日记
│   └── notifications/     # 通知相关实现
├── views/dashboard/       # 管理端与用户端页面、JS 与 CSS
├── tests/                 # 账本、服务流程与社区回归测试
├── requirements.txt       # Python 依赖
└── data/                  # 首次启动生成，不提交到 Git
```

交易账本、结算、报价、校验与用户模块由久留美插件源码移植；Agent 使用 LangChain 组装，并复用调整后的中文上下文估算和截断逻辑。独立服务通过 HTTP API 为浏览器工作区提供数据。

## 测试与演示验收

安装依赖后，在仓库目录运行自动化测试：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
```

Linux / macOS 使用 `.venv/bin/python -m unittest discover -s tests -q`。测试覆盖模拟账本、登录与模型调用流程、社区权限、未读边界和旧数据库迁移等；模型流程使用本地测试接口，不代表真实服务商已联调。

建议用两个用户账号、两个浏览器会话完成一次演示验收：

1. **交易与角色**：检查首页账户；连续发送两条消息确认上下文；触发一次看盘，核对持仓与交易日志。
2. **论坛**：发布带标签帖子，另一账号搜索并评论，确认详情、列表与本人删除行为一致。
3. **群聊**：创建群并让另一账号加入，发送消息，核对未读增加、打开后已读、退出与群主解散。
4. **AI 草稿**：生成复盘或论坛草稿，编辑后确认发布；取消时不应新增帖子。

## 数据保存与部署

默认数据目录包含：

| 文件 | 内容 |
| --- | --- |
| `data/ledger.sqlite3` | 用户、模拟账本、聊天、社区、模型配置与 API Key |
| `data/admin.json` | 管理员账号与密码校验信息，不保存明文密码 |
| `data/kurumi.log` | 运行日志；单文件 1 MiB，保留 3 份轮转备份 |

数据目录、虚拟环境和 SQLite 文件已加入 `.gitignore`。备份时先停止服务，再复制整个数据目录；不要仅复制运行中的数据库主文件而遗漏 WAL 等相关文件。需要全新演示数据时，将 `KURUMI_DATA` 指向新目录即可，保留原目录以便恢复。

两个服务都只监听 `127.0.0.1`。如需向外提供用户面板，可通过 Caddy 反向代理用户端口，管理端口继续保留在本机：

```caddyfile
panel.example.com {
    reverse_proxy 127.0.0.1:8791
}
```

将域名替换为自己的域名，并在启动前设置 `KURUMI_PUBLIC_URL=https://panel.example.com`。域名解析、HTTPS 与反向代理需要单独配置，本项目不会自动完成部署。

## 常见问题

| 现象 | 排查方式 |
| --- | --- |
| 启动提示缺少模块 | 使用启动服务的同一个 Python 执行 `-m pip install -r requirements.txt` |
| 8790 / 8791 端口被占用 | 检查是否已经启动过服务，停止旧进程后重试 |
| 忘记管理员首次设置 Token | 初始化前查看当前服务终端输出；重启会生成新的 Token。已初始化后使用管理员账号密码登录 |
| 提示先设置模型和 API Key | 检查是否默认进入真实行情模式；纯演示需设置 `KURUMI_LIVE=0` 后重启 |
| 模型地址被拒绝 | 检查允许列表、HTTPS 或回环地址限制，以及末尾 `/` 是否一致 |
| 真实行情为空或自动看盘未执行 | 检查行情源可用性、市场休市、自动看盘开关、执行间隔和休息时段，并查看日志 |
| 登录后再次被要求登录 | 检查是否打开了会清除会话的用户根路径，以及公开地址与 HTTP/HTTPS 是否匹配 |
| 离线模式仍请求模型 | 已保存模型与 Key 时会优先调用真实模型；纯演示需在界面清除 Key |

## 当前边界

- 当前以本机演示和桌面浏览器使用为主，移动端尚未专门适配。
- 群聊页面显示最近 80 条消息，暂不支持加载更早历史；群列表最多返回 100 个群，尚无分页。
- 社区暂不提供管理员审核、举报、禁言、封禁等治理功能。
- 真实模型及行情依赖外部服务，需要使用自己的配置单独联调；离线测试通过不等于真实服务可用。
- 复盘中的 `fills` 表示开仓和平仓操作数之和，不等于已完成交易轮数。
- 本项目用于学习、实验和演示，模拟结果不代表真实交易收益。
