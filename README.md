# 久留美独立 Demo

需要 Python 3.10 或更新版本。在本目录运行：

```powershell
python -m pip install -r requirements.txt
python app.py
```

1. 打开管理员入口。首次输入终端打印的一次性 Token，并设置管理员账号和密码。之后使用账号密码登录。账号和密码哈希保存在数据目录的 admin.json。
2. 创建用户和交易实例，然后在用户入口用生成的账号和密码登录。
3. 在用户面板保存自己的角色设定、模型名称和 API Key。API Key 留空时保留旧值。
4. 发送两条消息可以查看上下文是否延续。开启定时看盘的实例会自动写入模拟账本。

默认使用离线行情。没有设置模型时使用确定性的演示回复和模拟开仓，有设置时用真实模型接口。离线任务间隔为 30 秒。设置环境变量 `KURUMI_LIVE=1` 后接入久留美原有默认报价源，任务间隔为一小时。真实报价依赖行情源是否可用。

默认模型端点是 `https://api.openai.com/v1`。管理员可以用 `KURUMI_MODEL_URLS` 配置逗号分隔的允许列表，再由用户选填自己的接口地址。请保护 `data/ledger.sqlite3`，其中包含 API Key。网页不会回显已保存的 Key。

## 暴露用户面板

两个监听口都绑定本机。管理员页使用 8790，用户页使用 8791。将以下示例中的域名换成自己的域名，交给 Caddy，并在启动 Demo 前把 `KURUMI_PUBLIC_URL` 设置为对应的 HTTPS 地址。管理页会显示此链接，用户登录 Cookie 会采用 HTTPS。

```caddyfile
panel.example.com {
    reverse_proxy 127.0.0.1:8791
}
```

## 复用和范围

交易账本、结算、报价、交易校验和用户账号由久留美插件的源码移植。Agent 沿用 ForumAgent 的 LangChain 组装方式，直接复用并调整了中文上下文估算及截断实现。Demo 使用按实例保存的 SQLite 聊天记录作为上下文，每次读取最近 20 条。

第一版每次创建一个用户与一个实例。定时任务可以模拟开仓并记录观察结果。管理页使用简版界面，面板不包含完整图表、日记、聊天平台通知或自动配置反代服务。默认创建新账本。

```powershell
python -m unittest discover -s tests -q
```

