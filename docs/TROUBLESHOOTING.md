# Troubleshooting（排错指南）

本文记录在部署和使用过程中踩过的坑，以及对应的诊断方法与修复手段。
按「现象 → 根因 → 修复」组织，方便新同事和新同事的智能体快速定位。

> 相关文档：[SETUP.zh-CN.md](SETUP.zh-CN.md)、[MIGRATION-IMAP.md](MIGRATION-IMAP.md)、
> [DESIGN.md](DESIGN.md)。

---

## 1. 打开 Thunderbird 就报「连接 mail.example.com 错误」/ SSL_ERROR_UNSUPPORTED_VERSION

### 现象
- 一打开 TB，控制台报：
  ```
  An error occurred connecting to mail.example.com: SSL_ERROR_UNSUPPORTED_VERSION
  ```
- IMAP 无法收件（发件也受影响）。

### 根因（两层叠加）
1. **TLS 版本不兼容（主因）**：Exchange 2010（IIS 7.5）只支持 **TLS 1.0**。
   用 `openssl s_client` 实测：
   ```bash
   openssl s_client -connect mail.example.com:993 -tls1    # ✅ 成功
   openssl s_client -connect mail.example.com:993 -tls1_2  # ❌ 失败
   ```
   而新版 Thunderbird（Firefox/NSS 内核）默认 `security.tls.version.min=3`
   （TLS 1.2 起步），直接禁用了 TLS 1.0 → 握手失败。
2. **系统代理干扰（诱因）**：本机若开着 clash/mihomo 等代理（系统代理指向
   `127.0.0.1:7890`），TB 默认「使用系统代理」，内网邮件流量可能被错误地
   路由到代理。之前在家能连，是因为代理做了 TLS 桥接（代理与 TB 用新 TLS，
   代理与服务器用老 TLS），掩盖了 TLS 版本不兼容。

### 修复
在 TB 里做两处设置（`about:config` 或 `prefs.js`）：

```javascript
// 1. 允许 TLS 1.0（Exchange 2010 只支持到 TLS 1.0）
user_pref("security.tls.version.min", 1);

// 2. 不使用代理（内网邮箱 + 本地桥都不需要代理）
user_pref("network.proxy.type", 0);
```

也可在 UI 里设置代理：`设置 → 通用 → 网络与磁盘空间 → 连接 → 设置... → 不使用代理`。

> **注意**：改 `prefs.js` 前必须**完全退出 Thunderbird**，否则退出时会被覆盖。

### 验证
```bash
# 直连 IMAP 应能建立连接
ss -tnp | grep ':993'   # 应看到 thunderbird 到 <exchange-ip>:993 的 ESTAB 连接
```

---

## 2. SMTP 发件认证失败（无法向发件服务器认证）

### 现象
```
以用户名 xxx@mail.example.com 登录服务器 mail.example.com 失败。
无法向发件服务器（SMTP）进行认证。
```

### 根因
认证方式不匹配。Exchange 的 SMTP 支持 `AUTH LOGIN`（普通密码），**不需要 NTLM**。

### 修复
SMTP 服务器设置：
| 设置 | 值 |
|------|------|
| 服务器 | `mail.example.com` |
| 端口 | **587** |
| 连接安全性 | **STARTTLS** |
| 认证方式 | **普通密码**（不是 NTLM / 加密密码） |
| 用户名 | 完整邮箱 `xxx@mail.example.com` |

对应 `prefs.js`：
```javascript
user_pref("mail.smtpserver.smtp1.authMethod", 3);      // 3 = 普通密码 (AUTH LOGIN)
user_pref("mail.smtpserver.smtp1.hostname", "mail.example.com");
user_pref("mail.smtpserver.smtp1.port", 587);
user_pref("mail.smtpserver.smtp1.try_ssl", 2);          // 2 = STARTTLS
user_pref("mail.smtpserver.smtp1.username", "xxx@mail.example.com");
```

### 命令行验证（可选）
```bash
python3 - <<'EOF'
import smtplib, ssl
s = smtplib.SMTP("mail.example.com", 587, timeout=30)
s.starttls(context=ssl.create_default_context())
s.login("xxx@mail.example.com", "<password>")   # 应输出 235 Authentication successful
s.quit()
EOF
```

---

## 3. 发件箱/草稿箱对不上（本地有，Web 界面没有）

### 现象
- TB 里发出的邮件在本地「发件箱」里，但 Web 界面（OWA）的发件箱里看不到。
- 或有多个「发件箱」「草稿箱」，名称对不上。

### 根因
Thunderbird 用 **IMAP Modified UTF-7** 编码中文文件夹名，而服务端实际是英文
文件夹名。两者不一致 → TB 把邮件存到错误/本地文件夹，而非服务端的 `Sent`。

### 修复
在「账户设置 → 副本与文件夹」里，把「已发送邮件副本」「草稿箱」手动指向
服务端的英文文件夹 `Sent` / `Drafts`；或直接改 `prefs.js`：

```javascript
user_pref("mail.identity.id2.fcc_folder",
          "imap://xxx%40mail.example.com@mail.example.com/Sent");
user_pref("mail.identity.id2.draft_folder",
          "imap://xxx%40mail.example.com@mail.example.com/Drafts");
```

### 文件夹映射对照
| Exchange 中文名 | IMAP 标准名 |
|----------------|-------------|
| 收件箱 | INBOX |
| 已发送邮件 | Sent |
| 草稿 | Drafts |
| 已删除邮件 | Trash |
| 垃圾邮件 | Junk |

---

## 4. 日历提示「has a dangling E-Mail identity configured」

### 现象
控制台告警：
```
calendar: Calendar http://127.0.0.1:17081/dav/.../exchange/ has a dangling
E-Mail identity configured.
```

### 根因
日历关联的邮件身份（`calendar.registry.<id>.imip.identity.key`）指向一个
已不存在的账户身份（例如从 EWS 账户切换到 IMAP 账户后，旧的 `id1` 已删除，
但日历还引用着它）。

### 影响
不修复会导致**发送会议邀请失败**（日历找不到有效的发件人身份）。

### 修复
把日历的 `imip.identity.key` 指向当前有效的身份（IMAP 账户的 `id2`）：

```javascript
user_pref("calendar.registry.<日历id>.imip.identity.key", "id2");
```

> 先在 `prefs.js` 里确认当前有效身份的 key（`mail.identity.idN.valid = true` 的那个）。

---

## 5. 日历每次启动被强制只读

### 现象
重启 TB 后 Exchange 日历总是只读，手动取消只读后重启又被覆盖。

### 根因
桥端 `PROPFIND` 的 `current-user-privilege-set` 只宣告了 `<d:read/>`，TB 据此
判定无写权限，强制置只读并写回 `prefs.js`。

### 修复
桥端（`ewscaldav.py`）的日历集合与 home 发现条目需宣告
`<d:write/> <d:write-content/> <d:write-properties/> <d:bind/> <d:unbind/>`。
本仓库已实现，无需额外处理。

---

## 6. 日历显示为空（订阅了但没事件）

### 根因
Thunderbird 的 CalDAV 日历默认关闭「离线支持」，关闭时不会拉取/缓存远端事件。

### 修复
右键日历 → 属性 → 勾选 **「离线支持」(Offline Support)**。

---

## 7. 点未读邮件不立即变已读（仅旧 EWS 模式）

> 仅历史 EWS 邮件桥模式会遇到；IMAP 模式下无此问题。

### 根因
TB 原生 EWS 发出的 `UpdateItem` 不带 `ChangeKey`，EWS 2010 写校验一律拒绝。

### 修复
已在 `ews_bridge.py` 实现 ChangeKey 注入（`ensure_changekeys`）。IMAP 模式下
无需关心。

---

## 8. 转发带图邮件收件人看不到图片（仅旧 EWS 模式）

> 仅历史 EWS 邮件桥模式会遇到；IMAP 模式下无此问题。

### 根因
TB 把 `cid:` 引用改写成私有 `x-moz-ews://` URL 直接塞进出站 MimeContent，
未还原为 `cid:`+附件。

### 修复
已在 `ews_bridge.py` 实现内联图片修复（`fix_forward_images`）。IMAP 模式下
无需关心。

---

## 通用诊断技巧

### 查看 Thunderbird 错误控制台
```
工具 → 开发者工具 → 错误控制台（Ctrl+Shift+J）
```

### 检查 IMAP 连接
```bash
ss -tnp | grep ':993'          # 看 TB 是否直连成功
ss -tnp | grep ':7890'         # 看是否还走代理（clash）
```

### 测试 TLS 版本
```bash
for v in tls1 tls1_1 tls1_2 tls1_3; do
  echo "=== $v ==="
  timeout 5 openssl s_client -connect mail.example.com:993 -$v 2>&1 | grep -E "Protocol|Cipher"
done
```

### 测试 SMTP 认证
```bash
python3 -c "
import smtplib, ssl
s = smtplib.SMTP('mail.example.com', 587, timeout=30)
s.starttls(context=ssl.create_default_context())
print(s.ehlo())
"
```

---

*文档版本: 1.0.0 | 更新日期: 2026-10-16 | 维护者: gic-commits*
