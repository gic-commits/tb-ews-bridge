# DavMail 访问 Exchange In-Place Archive 配置指南

## 适用场景
- Exchange 2010/2013/2016/2019 + In-Place Archive（个人存档/Online Archive）
- Thunderbird 需通过 IMAP 访问存档邮件
- 环境：Linux (麒麟/Ubuntu/Debian) + Flatpak 或二进制包

---

## 架构概览

```
Thunderbird (双账户)
├── 账户 1：主邮箱 → mail.example.com:993 (直连 IMAP/SMTP)
└── 账户 2：个人存档 → 127.0.0.1:1143 (DavMail 网关 + /archive 前缀)

DavMail 网关 (systemd 用户服务)
├── 监听: 127.0.0.1:1143 (IMAP), 1025 (SMTP)
├── 协议: EWS (HTTPS) → mail.example.com/ews/exchange.asmx
├── 认证: NTLM
└── 模式: -server (headless 后台运行)
```

---

## 1. DavMail 安装

### 方案 A：Flatpak (推荐，无需 root)
```bash
flatpak install flathub org.davmail.DavMail
```

### 方案 B：官方二进制包 (需完整 JDK + SWT)
```bash
# 下载
wget https://sourceforge.net/projects/davmail/files/davmail/7.1.0/davmail-7.1.0-4498.zip
unzip davmail-7.1.0-4498.zip -d davmail

# 依赖: openjdk-11-jdk + libswt-gtk-4-java (或自带 SWT)
```

### 方案 C：Docker (最简单)
```bash
docker run -d --name davmail \
  -p 1143:1143 -p 1025:1025 \
  -e DAVMAIL_URL=https://mail.example.com/ews/exchange.asmx \
  -e DAVMAIL_MODE=IMAP \
  -e DAVMAIL_AUTHENTICATION=NTLM \
  -e DAVMAIL_SERVER=mail.example.com \
  -e DAVMAIL_DOMAIN=your-company.com \
  -e DAVMAIL_ALLOW_REMOTE=true \
  davmail/davmail
```

---

## 2. 配置文件

### 位置
- Flatpak: `~/.var/app/org.davmail.DavMail/config/davmail/davmail.properties`
- 二进制包: `davmail.properties` (同目录)

### 核心配置 (davmail.properties)
```properties
# EWS 连接
davmail.url=https://mail.example.com/ews/exchange.asmx
davmail.mode=IMAP
davmail.authentication=NTLM
davmail.server=mail.example.com
davmail.domain=your-company.com
davmail.allowRemote=true

# 端口 (仅启用 IMAP/SMTP)
davmail.imapPort=1143
davmail.smtpPort=1025
davmail.caldavPort=0
davmail.ldapPort=0
davmail.popPort=0

# 禁用 SSL (本地连接无需加密)
davmail.ssl.port=0
davmail.ssl.nosecureimap=true
davmail.ssl.nosecuresmtp=true

# 后台模式
davmail.enableTray=false
davmail.bindAddress=0.0.0.0

# 稳定性优化
davmail.keepDelay=60
davmail.enableKeepAlive=true
davmail.disableUpdateCheck=true
davmail.clientSoTimeout=0
```

---

## 3. Systemd 用户服务 (后台持久化)

### 创建服务文件
```bash
mkdir -p ~/.config/systemd/user

cat > ~/.config/systemd/user/davmail.service << 'EOF'
[Unit]
Description=DavMail Gateway
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/flatpak run org.davmail.DavMail /home/greatwall/.var/app/org.davmail.DavMail/config/davmail/davmail.properties -server
Restart=always
RestartSec=10
Environment=DISPLAY=:0

[Install]
WantedBy=default.target
EOF
```

### 启用服务
```bash
systemctl --user daemon-reload
systemctl --user enable davmail
systemctl --user start davmail

# 查看状态
systemctl --user status davmail
journalctl --user -u davmail -f
```

### 验证端口
```bash
ss -ltnp | grep -E '1143|1025'
# 应显示 java 进程监听 1143 和 1025
```

---

## 4. Thunderbird 双账户配置

### 账户 1：主邮箱 (直连，推荐)
```
账户类型: IMAP
姓名: 你的名字
邮箱地址: you@your-company.com
密码: ********

收件服务器 (IMAP):
  服务器: mail.example.com
  端口: 993
  连接安全性: SSL/TLS
  认证方式: 普通密码

发件服务器 (SMTP):
  服务器: mail.example.com
  端口: 587
  连接安全性: STARTTLS
  认证方式: 普通密码
```

### 账户 2：个人存档 (DavMail 网关)
```
账户类型: IMAP
姓名: 你的名字 (Archive)
邮箱地址: you@your-company.com
密码: ********

收件服务器 (IMAP):
  服务器: 127.0.0.1
  端口: 1143
  连接安全性: 无 (本地连接)
  认证方式: 普通密码

  高级设置 → IMAP 服务器目录: /archive

发件服务器 (SMTP):
  服务器: 127.0.0.1
  端口: 1025
  连接安全性: 无
  认证方式: 普通密码
```

### 关键步骤：订阅存档文件夹
1. 右键账户 2 → **订阅...**
2. 勾选所有 `/archive/...` 文件夹：
   - `/archive/收件箱`
   - `/archive/已发送邮件`
   - `/archive/草稿`
   - 其他业务文件夹 (如 `/archive/禅道CI159`)
3. 取消勾选主邮箱文件夹 (Inbox, Sent, Drafts 等)

---

## 5. 存档文件夹路径原理

DavMail 将 In-Place Archive 暴露为 `/archive` 层级：

```
IMAP LIST "" "*" 返回:
/archive/收件箱
/archive/已发送邮件
/archive/草稿
/archive/已删除邮件
/archive/禅道CI159
...
```

Thunderbird 设置 **IMAP 服务器目录 = `/archive`** 后，客户端自动将所有操作映射到该前缀下。

---

## 6. 常见问题排查

### DavMail 启动后自动退出
- **原因**: 未使用 systemd 管理，或配置文件路径错误
- **解决**: 必须用 `systemd-run --user --scope` 或 systemd 服务启动

### 无法连接 Exchange
- 检查 `davmail.url` 是否正确 (必须含 `/ews/exchange.asmx`)
- 确认 NTLM 认证支持 (Exchange 2010 需 NTLM)
- 检查防火墙/网络连通性

### 存档文件夹不显示
- 确认 Thunderbird 高级设置中 **IMAP 服务器目录 = `/archive`**
- 右键账户 → 订阅 → 刷新文件夹列表

### 中文文件夹名显示乱码
- DavMail 自动处理 IMAP UTF-7 编码
- Thunderbird 通常能正确显示

### 认证失败
- 尝试 `davmail.authentication=NTLM` 或 `Basic`
- 确认域名格式: `your-company.com` 或留空
- 用户名格式: `you@your-company.com` 或 `your-company.com\your-username`

---

## 7. 维护命令速查

```bash
# 服务管理
systemctl --user status davmail
systemctl --user restart davmail
systemctl --user stop davmail

# 日志查看
journalctl --user -u davmail -f
journalctl --user -u davmail --since "1 hour ago"

# 端口检查
ss -ltnp | grep -E '1143|1025'

# 配置修改后重启
vim ~/.var/app/org.davmail.DavMail/config/davmail/davmail.properties
systemctl --user restart davmail

# 测试 IMAP 连接
python3 -c "
import socket
s = socket.create_connection(('127.0.0.1', 1143), timeout=5)
print(s.recv(1024).decode())
s.sendall(b'a1 LOGIN you@your-company.com PASSWORD\r\n')
print(s.recv(1024).decode())
s.sendall(b'a2 LIST \"\" \"*\"\r\n')
while True:
    data = s.recv(1024)
    print(data.decode())
    if b'a2 OK' in data: break
"
```

---

## 8. 完整部署清单

- [ ] Flatpak 安装 DavMail
- [ ] 创建配置文件 (`davmail.properties`)
- [ ] 创建 systemd 用户服务
- [ ] 启用并启动服务
- [ ] 验证端口监听 (1143, 1025)
- [ ] Thunderbird 创建主邮箱账户 (直连)
- [ ] Thunderbird 创建存档账户 (DavMail + `/archive`)
- [ ] 订阅存档文件夹
- [ ] 测试收发邮件
- [ ] 配置开机自启 (`systemctl --user enable davmail`)

---

## 9. 参考资源

- DavMail 官网: http://davmail.sourceforge.net/
- Exchange In-Place Archive 架构: https://learn.microsoft.com/exchange/architecture/in-place-archive
- Thunderbird IMAP 路径前缀: https://support.mozilla.org/kb/imap-folders-thunderbird

---

## 更新记录

| 日期 | 版本 | 说明 |
|------|------|------|
| 2026-11-03 | 1.0 | 初版：DavMail server 模式 + Thunderbird 双账户 + `/archive` 前缀方案 |

---

> **核心心法**：单网关、双账户、路径前缀区分。DavMail 只需跑一个实例，Thunderbird 通过 IMAP 路径前缀自动区分主邮箱与存档。
