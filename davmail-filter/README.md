# DavMail 代理配置指南（IMAP Archive 过滤）

## 概述

本项目使用 **DavMail** 作为 Exchange 2010 EWS 网关，并在其前面部署了一个 **DavMail IMAP Archive Filter 代理**（`davmail-filter.py`），用于在 Thunderbird 中仅暴露个人存档（Online Archive / In-Place Archive）文件夹，隐藏主邮箱文件夹。

## 架构概览

```
Thunderbird (IMAP 账户)
   └── 端口 17143  ——> [DavMail Archive Filter 代理] ——> 端口 1143 ——> DavMail 网关 ──> Exchange 2010 EWS
```

- **DavMail 网关**：端口 1143，连接 Exchange 2010 EWS，暴露所有文件夹（主邮箱 + 个人存档）
- **DavMail 代理**：端口 17143，位于 Thunderbird 和 DavMail 之间，仅透传 `/archive/` 前缀的文件夹，拦截其他文件夹

## 核心配置

### 1. DavMail 配置（`~/.var/app/org.davmail.DavMail/config/davmail/davmail.properties`）

关键配置项：

```properties
davmail.url=https://mail.your-company.com/ews/exchange.asmx
davmail.mode=IMAP
davmail.authentication=NTLM
davmail.server=mail.your-company.com
davmail.domain=your-company.com
davmail.imapPort=1143
davmail.smtpPort=1025
davmail.enableTray=false
davmail.bindAddress=0.0.0.0
davmail.enableTray=false
```

### 2. DavMail 代理配置（`~/.var/app/org.davmail.DavMail/config/davmail/davmail.properties`）

```properties
# IMAP 服务器（Exchange EWS 网关）
davmail.url=https://mail.your-company.com/ews/exchange.asmx
davmail.mode=IMAP
davmail.authentication=NTLM
davmail.server=mail.your-company.com
davmail.domain=your-company.com
davmail.allowRemote=true
davmail.imapPort=1143
davmail.smtpPort=1025
davmail.caldavPort=0
davmail.ldapPort=0
davmail.popPort=0
davmail.ssl.port=0
davmail.ssl.nosecureimap=true
davmail.ssl.nosecuresmtp=true
davmail.enableTray=false
davmail.bindAddress=0.0.0.0
davmail.keepDelay=60
davmail.enableKeepAlive=true
davmail.disableUpdateCheck=true
davmail.clientSoTimeout=0

# 日志配置（避免写到用户主目录）
davmail.log=true
davmail.logFile=/tmp/davmail.log
```

### 3. DavMail 代理配置（`~/.var/app/org.davmail.DavMail/config/davmail/davmail-filter/davmail_filter.conf`）

```ini
[davmail_filter]
# 上游 DavMail 服务器
upstream_host = 127.0.0.1
upstream_port = 1143

# 本地监听端口
listen_port = 17143

[filter]
# 模式：
#   whitelist：只保留下面规则匹配的文件夹，其余丢弃
#   blacklist：只丢弃下面规则匹配的文件夹，其余保留
mode = whitelist

# 匹配规则：
#   prefix:/xxx   前缀匹配（文件夹名以 /xxx 开头）
#   exact:/xxx    精确匹配（文件夹名完全等于 /xxx）
#   regex:/xxx/   正则表达式匹配
rules =
    exact:archive
    prefix:/archive/
```

配置说明：
- `davmail_filter`：DavMail 网关地址和端口
- `davmail_filter`：DavMail 网关代理监听端口
- `mode=whitelist`：仅保留规则匹配的文件夹
- `rules`：
  - `exact:archive`：匹配文件夹名完全等于 `archive`（IMAP 根目录下的存档文件夹）
  - `prefix:/archive/`：匹配所有以 `/archive/` 开头的子文件夹（存档下的子文件夹）

### 4. systemd 用户服务（`~/.config/systemd/user/davmail-filter.service`）

```ini
[Unit]
Description=DavMail IMAP Archive Filter Proxy
After=network.target davmail.service
Wants=davmail.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 /home/greatwall/davmail-filter/davmail_filter.py
Restart=always
RestartSec=5
WorkingDirectory=/home/greatwall/davmail-filter

[Install]
WantedBy=default.target
```

启用与启动：
```bash
systemctl --user daemon-reload
systemctl --user enable davmail-filter
systemctl --user start davmail-filter
```

---

## Thunderbird 配置

### 账户 1：主邮箱（直连 IMAP/SMTP，不走 DavMail）
| 字段 | 值 |
|------|-----|
| 服务器 | mail.your-company.com |
| 端口 | 993 (IMAP) / 587 (SMTP) |
| 连接安全性 | SSL/TLS (IMAP), STARTTLS (SMTP) |
| 认证方式 | 普通密码 |

### 账户 2：个人存档（通过 DavMail 代理）
| 字段 | 值 |
|------|-----|
| 服务器 | 127.0.0.1 |
| 端口 | 1143 |
| 连接安全性 | 无（本地回环） |
| 认证方式 | 普通密码 |
| 用户名 | your-email@your-company.com |
| 密码 | 密码 |
| 高级 → IMAP 服务器目录 | `/archive` |

> **重要**：IMAP 服务器目录必须填写 `/archive`，否则 Thunderbird 会尝试访问主邮箱文件夹，被代理拦截。

---

## 工作原理

1. Thunderbird 通过 IMAP 协议连接 `127.0.0.1:17143`（代理端口）
2. 代理转发请求到 `127.0.0.1:1143`（DavMail 网关）
3. DavMail 通过 EWS 连接 Exchange 2010，获取所有文件夹列表
4. 代理过滤 IMAP LIST/LSUB 响应，只保留路径以 `/archive/` 开头或完全等于 `archive` 的文件夹
5. 非存档文件夹的 SELECT/EXAMINE/STATUS 等操作被拦截，返回空文件夹响应

## 常见问题

### Q: 为什么看不到任何文件夹？
A: 检查配置文件 `davmail_filter.conf` 中的规则是否正确，以及 Thunderbird 的 IMAP 服务器目录是否设置为 `/archive`。

### Q: 为什么某些存档文件夹不显示？
A: 检查文件夹名称是否以 `/archive/` 开头，或等于 `archive`。可以通过 `LIST "" "*"` 命令查看实际返回的文件夹列表。

### Q: 为什么访问 INBOX 报错？
A: 这是预期行为。非存档文件夹的访问被代理拦截，返回空文件夹响应，Thunderbird 将显示该文件夹为空。

---

## 文件结构

```
/home/greatwall/davmail-filter/
├── davmail_filter.py          # DavMail IMAP 代理主程序
├── davmail_filter.conf        # 配置文件
└── README.md                  # 本文档
```

---

## 更新日志

| 日期 | 版本 | 说明 |
|------|------|------|
| 2026-11-06 | 1.0 | 初版：DavMail Bridge 代理，支持 IMAP 存档文件夹过滤 |

---

## 注意事项

1. **不要提交包含真实邮箱地址、密码、服务器地址的配置文件到公共仓库**
2. 配置文件中的邮箱地址和服务器地址应替换为占位符
3. 修改配置文件后，需重启 `davmail-filter` 服务：`systemctl --user restart davmail-filter`
4. 修改 Thunderbird 账户配置后，需要重启 Thunderbird 才能生效