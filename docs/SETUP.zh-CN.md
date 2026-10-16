# 搭建与使用指南（从零开始）

> English: [SETUP.md](SETUP.md)

本文档面向**新同事 / 新同事的智能体**，目标是：在一台**全新的 Linux 设备**上，
按照本文步骤即可把整套「Thunderbird ↔ Exchange 2010」接入方案搭建并配置成功。

## 当前架构（重要）

**邮件用原生 IMAP/SMTP，日历/通讯录用本地桥**：

| 功能 | 协议/端口 | 说明 |
|------|-----------|------|
| 邮件（收） | IMAP `993` (SSL/TLS) | Thunderbird 原生，无需桥 |
| 邮件（发） | SMTP `587` (STARTTLS) | Thunderbird 原生，无需桥 |
| 日历 | CalDAV `17081` | 本地桥 `ewscaldav.py` → EWS |
| 通讯录 | LDAP `17089` | 本地桥 `ldapgal.py` → EWS |

> 历史 EWS 邮件桥（`ews_bridge.py:17080`）已停用，仅保留代码备用，本文不再介绍。
> 如需从旧 EWS 模式迁移，见 [MIGRATION-IMAP.md](MIGRATION-IMAP.md)。

---

## 0. 前置条件

- Linux 桌面（Kylin / Ubuntu / 其它发行版均可，ARM aarch64 与 x86_64 都验证过）。
- **Python 3.8+**（仅标准库，无第三方依赖）和 **`curl`**。
- 一个 **Exchange 2010** 账号（邮箱 + 密码），且：
  - IMAP/SMTP 服务可用（`mail.example.com:993` / `mail.example.com:587`）；
  - 能用浏览器登录 `https://mail.example.com/owa/`；
  - 内网 DNS 能解析 `mail.example.com`。

> **关键认知**：Exchange 2010 是老旧服务器（IIS 7.5），只支持 **TLS 1.0**。
> 这点会导致新版 Thunderbird 直连失败，详见 §5 的「TLS 版本」配置。

---

## 1. 安装 Thunderbird

```bash
sudo snap install thunderbird        # 或 sudo apt install thunderbird
```

**版本要求**：需要支持原生 **CalDAV / LDAP** 账户类型（现代 TB 均可）。

> 不需要任何第三方插件。邮件走原生 IMAP/SMTP，日历/通讯录走原生 CalDAV/LDAP。

---

## 2. 部署本地桥（日历 + 通讯录）

### 2.1 拉取代码并部署到固定路径

> **重要**：脚本必须放在一个**不会被系统清理**的固定路径（如 `/opt/ews-bridge/`），
> 不要放 `~/bin`（曾因系统升级被清空导致服务失效）。

```bash
git clone https://github.com/<your-org>/tb-ews-bridge.git
cd tb-ews-bridge

sudo mkdir -p /opt/ews-bridge
sudo cp ewscaldav.py ldapgal.py ews_bridge.py /opt/ews-bridge/
sudo chmod +x /opt/ews-bridge/*.py
sudo chown -R $(whoami):$(whoami) /opt/ews-bridge
```

> 说明：`ewscaldav.py` / `ldapgal.py` 会 `import ews_bridge.py`（复用其 EWS 通道），
> 所以 `ews_bridge.py` 也要一起部署（即使不作为服务运行）。

### 2.2 配置凭据

```bash
mkdir -p ~/.config/ews-bridge
cp cred.json.example ~/.config/ews-bridge/cred.json
$EDITOR ~/.config/ews-bridge/cred.json    # 填入真实值
chmod 600 ~/.config/ews-bridge/cred.json
```

`cred.json` 字段说明：

| 字段 | 含义 |
|------|------|
| `host` / `port` / `sni` | Exchange 主机名、443、SNI（通常同主机名） |
| `user` / `password` | 域账号（UPN，如 `you@example.com`）与密码 |
| `listen` / `log` | 监听地址 `127.0.0.1` / 日志文件 |
| `ldap_port` | LDAP 桥端口，默认 `17089` |
| `ldap_base` | GAL Base DN，如 `dc=example,dc=com` |

> `cred.json` 已 gitignore，请勿提交。

### 2.3 配置 systemd 用户服务

```bash
mkdir -p ~/.config/systemd/user
cp systemd/ewscaldav.service systemd/ldapgal.service ~/.config/systemd/user/

# 确保 ExecStart 指向 /opt/ews-bridge/（仓库模板默认已是）
systemctl --user daemon-reload
systemctl --user enable --now ewscaldav.service ldapgal.service
```

> **不要**启用 `ews-bridge.service`（邮件已走 IMAP/SMTP）。

### 2.4 自检

```bash
ss -ltn | grep -E '17081|17089'     # 两个端口应监听

# CalDAV 探测（期望 207）
curl -s -o /dev/null -w '%{http_code}\n' -X PROPFIND -H 'Depth: 0' \
  --data '<D:propfind xmlns:D="DAV:"><D:prop><D:resourcetype/></D:prop></D:propfind>' \
  http://127.0.0.1:17081/dav/you@example.com/exchange/
```

---

## 3. Thunderbird：创建 IMAP 邮件账户

1. **账户设置 → 新建 → 邮件**，输入姓名、邮箱、密码，继续。
2. Thunderbird 可能自动探测；若失败则**手动配置**：
   - 账户类型：**IMAP**（收件）
   - 收件服务器：`mail.example.com`，端口 `993`，**SSL/TLS**
   - 用户名：完整邮箱 `you@example.com`
3. 发件服务器（SMTP）：
   - 服务器：`mail.example.com`，端口 `587`，**STARTTLS**
   - 认证方式：**普通密码**（不是 NTLM）
   - 用户名：完整邮箱 `you@example.com`

> **SMTP 认证方式必须是「普通密码」**：Exchange 支持 `AUTH LOGIN`，不需要 NTLM。
> 详见 [TROUBLESHOOTING.md](TROUBLESHOOTING.md) §2。

---

## 4. 修复文件夹映射（发件箱 / 草稿箱）

Exchange 服务端用**英文**文件夹名（`Sent`、`Drafts`），而 Thunderbird 可能默认
用中文名（内部 UTF-7 编码）导致对不上。需要手动指定：

```
账户设置 → 副本与文件夹
→ 已发送邮件副本 → 其他文件夹 → 选择服务端 "Sent"
→ 草稿箱 → 其他文件夹 → 选择服务端 "Drafts"
```

等价 `prefs.js`（关闭 TB 后改）：
```javascript
user_pref("mail.identity.idN.fcc_folder",
          "imap://you%40example.com@mail.example.com/Sent");
user_pref("mail.identity.idN.draft_folder",
          "imap://you%40example.com@mail.example.com/Drafts");
```

> 不修复会导致：发出的邮件只存在本地，Web 界面的发件箱看不到。

---

## 5. 关键网络配置（TLS 版本 + 代理）

**这是 Exchange 2010 环境最容易踩的坑**，新设备务必检查。

### 5.1 允许 TLS 1.0

Exchange 2010（IIS 7.5）只支持 **TLS 1.0**，新版 Thunderbird 默认禁用它。

`about:config`（或关闭 TB 后改 `prefs.js`）：
```javascript
user_pref("security.tls.version.min", 1);
```

### 5.2 不使用代理

若系统开着 clash/mihomo 等代理，TB 默认「使用系统代理」会把内网邮件流量
错误路由到代理。

UI 设置：`设置 → 通用 → 网络与磁盘空间 → 连接 → 设置... → 不使用代理`
或 `prefs.js`：
```javascript
user_pref("network.proxy.type", 0);
```

> **改 `prefs.js` 前必须完全退出 Thunderbird**，否则退出时会被覆盖。

---

## 6. 添加 CalDAV 日历

1. **日历 → 新建日历 → 网络日历（CalDAV）**，URL：
   `http://127.0.0.1:17081/dav/you@example.com/exchange/`
2. 订阅后，**右键日历 → 属性 → 勾选「离线支持」**（否则日历为空）。

### 6.1 修复日历关联的邮件身份（重要）

若控制台报 `Calendar ... has a dangling E-Mail identity configured`，说明日历
关联的邮件身份失效（如从 EWS 账户切到 IMAP 后），需把日历的 `imip.identity.key`
指向当前有效身份：

```javascript
user_pref("calendar.registry.<日历id>.imip.identity.key", "id2");
```

> 不修复会导致**发送会议邀请失败**。详见 [TROUBLESHOOTING.md](TROUBLESHOOTING.md) §4。

---

## 7. 添加 LDAP 地址簿 + 写信自动补全

1. **地址簿 → 新建 → LDAP 目录**：
   - Host `127.0.0.1`，Port `17089`，Base DN `dc=example,dc=com`
   - 勾选「写信时在地址簿中查找」
2. **写信自动补全**：`设置 → 撰写 → 地址`，把 **Directory Server** 从 `None`
   改为该 LDAP 目录（等价 `about:config`：
   `ldap_2.autoComplete.useDirectory=true` +
   `ldap_2.autoComplete.directoryServer=ldap_2.servers.<name>`）。

---

## 8. 验证清单

- [ ] `ss -ltn` 显示 `17081` / `17089` 监听
- [ ] TB **收发邮件**正常（IMAP 收、SMTP 发）
- [ ] **发件箱**在 Web 界面（OWA）可见
- [ ] **日历**可建/改/删事件（不是只读），离线支持已勾选
- [ ] **地址簿**能搜到人，写信自动补全可用
- [ ] 控制台无 `SSL_ERROR_UNSUPPORTED_VERSION`、无 `dangling E-Mail identity` 告警

---

## 9. 常见问题速查

| 问题 | 快速定位 | 详见 |
|------|----------|------|
| 连接 mail.example.com 错误 / SSL_ERROR_UNSUPPORTED_VERSION | TLS 1.0 未启用 | [TROUBLESHOOTING](TROUBLESHOOTING.md) §1 |
| SMTP 认证失败 | 认证方式应为「普通密码」 | [TROUBLESHOOTING](TROUBLESHOOTING.md) §2 |
| 发件箱对不上 | 文件夹映射指向 Sent | [TROUBLESHOOTING](TROUBLESHOOTING.md) §3 |
| 日历悬空身份告警 | imip.identity.key 失效 | [TROUBLESHOOTING](TROUBLESHOOTING.md) §4 |
| 日历只读 | PROPFIND 写权限未宣告 | [TROUBLESHOOTING](TROUBLESHOOTING.md) §5 |
| 日历为空 | 离线支持未勾选 | [TROUBLESHOOTING](TROUBLESHOOTING.md) §6 |

完整排错指南见 [TROUBLESHOOTING.md](TROUBLESHOOTING.md)。

---

## 10. 卸载

```bash
systemctl --user disable --now ewscaldav.service ldapgal.service
sudo rm -rf /opt/ews-bridge
rm -f ~/.config/ews-bridge/cred.json
# Thunderbird 侧：删除账户 / 日历 / 地址簿即可
```

---

*文档版本: 2.0.0（IMAP 模式）| 更新日期: 2026-10-16 | 维护者: gic-commits*
