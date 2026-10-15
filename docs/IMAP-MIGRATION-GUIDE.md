# Thunderbird EWS 到 IMAP 迁移指南

## 背景

Thunderbird 原生 EWS 账户存在以下已知缺陷：
- 不支持服务端推送，轮询定时器不触发同步
- 发件箱/草稿箱文件夹映射错误（UTF-7 编码 vs 英文文件夹名）
- SMTP 认证方式不匹配（Exchange 支持 LOGIN，不需要 NTLM）
- 界面不自动刷新，需手动 F5

## 迁移方案：IMAP + EWS Bridge 混合模式

### 架构调整

```
┌─────────────────────────────────────────────────────────────┐
│                    Thunderbird                              │
├─────────────────────┬─────────────────────┬─────────────────┤
│   IMAP (993/587)    │   CalDAV (17081)    │   LDAP (17089)  │
│   邮件收发/同步/搜索  │   日历双向同步       │   通讯录搜索/补全  │
└─────────────────────┴─────────────────────┴─────────────────┘
         │                     │                    │
         ▼                     ▼                    ▼
    mail.example.com:993    ewscaldav.py:17081    ldapgal.py:17089
         │                     │                    │
         └─────────────────────┼────────────────────┘
                               ▼
                    Exchange 2010 (mail.example.com)
```

### 组件职责分离

| 组件 | 端口 | 职责 | 状态 |
|------|------|------|------|
| **IMAP/SMTP** | 993/587 | 邮件收发、同步、搜索、推送 | ✅ 主力 |
| **CalDAV (ewscaldav.py)** | 17081 | 日历双向同步 | ✅ 保留 |
| **LDAP (ldapgal.py)** | 17089 | 通讯录搜索/自动补全 | ✅ 保留 |
| **EWS Bridge (ews_bridge.py)** | 17080 | 邮件转发 | ❌ 停用 (保留代码) |

---

## IMAP 账户配置指南

### 1. 创建 IMAP 账户

```
账户类型: IMAP
服务器: mail.example.com
端口: 993
连接安全性: SSL/TLS
认证方式: 普通密码
用户名: your-email@mail.example.com (完整邮箱)
密码: 您的密码
```

### 2. SMTP 发件服务器配置 (关键)

| 设置 | 值 | 说明 |
|------|------|------|
| 服务器 | mail.example.com | |
| 端口 | 587 | STARTTLS 标准端口 |
| 连接安全性 | STARTTLS | 不是 SSL/TLS |
| 认证方式 | 普通密码 | 不是 NTLM，不是加密密码 |
| 用户名 | your-email@mail.example.com | 完整邮箱 |
| 密码 | 您的密码 | |

**关键点**: Exchange 支持 `AUTH LOGIN` (普通密码)，不需要 NTLM。Thunderbird 默认的"普通密码"即 `AUTH LOGIN`，完全兼容。

---

## 文件夹映射修复 (关键)

### 问题
Thunderbird 配置中的文件夹名称使用 IMAP Modified UTF-7 编码（中文文件夹名），而服务端实际使用英文文件夹名，导致：
- 发件箱保存到错误文件夹 (`&XfJT0ZABkK5O9g-` vs `Sent`)
- 草稿箱保存到错误文件夹 (`&g0l6Pw-` vs `Drafts`)

### 修复方法

#### 方法 1: 配置编辑器 (推荐)

```
工具 → 选项 → 常规 → 配置编辑器 → 我接受风险
搜索并修改：
mail.identity.id2.fcc_folder = imap://your-email%40mail.example.com@mail.example.com/Sent
mail.identity.id2.draft_folder = imap://your-email%40mail.example.com@mail.example.com/Drafts
```

#### 方法 2: 账户设置界面 (推荐新手)

```
账户设置 → 副本与文件夹
→ 已发送邮件副本 → 其他文件夹 → 选择服务器上的 "Sent"
→ 草稿箱 → 其他文件夹 → 选择服务器上的 "Drafts"
```

#### 方法 3: 配置文件直接修改 (高级)

```bash
# 关闭 TB 后修改 prefs.js
sed -i 's|imap://.*/&XfJT0ZABkK5O9g-|imap://your-email%40mail.example.com@mail.example.com/Sent|g' prefs.js
sed -i 's|imap://.*/&g0l6Pw-|imap://your-email%40mail.example.com@mail.example.com/Drafts|g' prefs.js
```

### 文件夹映射对照表

| Exchange 中文名 | IMAP 标准名 | TB 配置值 |
|----------------|-------------|-----------|
| 收件箱 | INBOX | INBOX (自动) |
| 已发送邮件 | Sent | Sent |
| 草稿 | Drafts | Drafts |
| 已删除邮件 | Trash | Trash |
| 垃圾邮件 | Junk | Junk |
| 存档 | Archive | Archive |
| 发件箱 | Outbox | Outbox |

---

## SMTP 发件配置 (关键)

### 正确配置

| 设置 | 值 | 说明 |
|------|------|------|
| 服务器 | mail.example.com | |
| 端口 | **587** | STARTTLS 标准端口 |
| 连接安全性 | **STARTTLS** | 不是 SSL/TLS |
| **认证方式** | **普通密码** | **不是 NTLM** |
| 用户名 | your-email@mail.example.com | 完整邮箱 |
| 密码 | 您的密码 | |

### 关键发现
- Exchange 支持 `AUTH LOGIN` (普通密码)，**不需要 NTLM**
- Thunderbird 默认的"普通密码"即 `AUTH LOGIN`，完全兼容
- 端口 587 + STARTTLS 是标准配置

---

## EWS Bridge 精简配置

### 服务状态调整

```bash
# 禁用邮件桥自启动 (保留代码)
systemctl --user disable ews-bridge.service

# 保留 CalDAV/LDAP 服务
systemctl --user enable --now ewscaldav ldapgal
```

### 服务架构调整后

| 服务 | 端口 | 状态 | 用途 |
|------|------|------|------|
| ews-bridge | 17080 | **disabled** | 邮件桥 (保留代码，不自启动) |
| ewscaldav | 17081 | enabled | CalDAV 日历 |
| ldapgal | 17089 | enabled | LDAP 通讯录 |

### 代码变更
- 删除 auto-sync 后台线程代码
- 回滚到"睡眠/苏醒"修复后的状态
- 代码保留，仅禁用自启动

---

## 常见问题与解决

### 1. 发件箱不在 Sent 文件夹
**原因**: fcc_folder 指向错误文件夹 (`&XfJT0ZABkK5O9g-` vs `Sent`)
**解决**: 修改 `mail.identity.id2.fcc_folder` 为 `Sent`

### 2. 草稿不在 Drafts 文件夹
**原因**: `draft_folder` 指向 `&g0l6Pw-` 而非 `Drafts`
**解决**: 修改 `draft_folder` 为 `Drafts`

### 3. SMTP 认证失败
**错误**: `无法向发件服务器（SMTP）进行认证`
**解决**: 改用 `普通密码` + `STARTTLS` + 端口 587，**不要用 NTLM**

### 4. 发件箱不在 Web 界面显示
**原因**: 本地发送成功但未同步到服务端 Sent 文件夹
**解决**: 修复 fcc_folder 指向正确的 Sent 文件夹

### 5. 界面不自动刷新
**原因**: TB 原生 EWS 不支持推送，轮询不触发
**解决**: 改用 IMAP (原生 IDLE 推送 + 3 分钟轮询)

---

## 维护操作

### 启动/停止服务
```bash
# 启动所有服务
systemctl --user start ewscaldav ldapgal

# 停用邮件桥 (如需)
systemctl --user stop ews-bridge

# 临时启用邮件桥
systemctl --user start ews-bridge
```

### 查看日志
```bash
# 邮件桥日志
tail -f /tmp/ews-bridge.log | grep -E "(AUTO_SYNC|SyncFolderItems|GetItem)"

# CalDAV 日志
grep CALD /tmp/ews-bridge.log

# LDAP 日志
grep LDGAL /tmp/ews-bridge.log
```

### 重启服务
```bash
systemctl --user restart ewscaldav ldapgal
```

---

## 回滚方案

如需恢复原 EWS 模式：
```bash
# 启用邮件桥
systemctl --user enable --now ews-bridge

# 删除 IMAP 账户，恢复 EWS 账户设置
# 注意：需恢复 fcc_folder、draft_folder 等偏好设置
```

---

## 版本历史

| 日期 | 版本 | 变更 |
|------|------|------|
| 2026-10-15 | v1.2.0 | IMAP 迁移，EWS Bridge 精简，文件夹映射修复 |
| 2026-10-15 | v1.1.0 | Auto-sync 线程，睡眠/唤醒修复 |
| 2026-09-24 | v1.0.0 | 初始版本 |

---

## 附录：配置文件快速参考

### prefs.js 关键配置
```javascript
// 发件箱
user_pref("mail.identity.id2.fcc_folder", "imap://your-email%40mail.example.com@mail.example.com/Sent");
user_pref("mail.identity.id2.fcc_folder_picker_mode", 1);

// 草稿箱
user_pref("mail.identity.id2.draft_folder", "imap://your-email%40mail.example.com@mail.example.com/Drafts");
user_pref("mail.identity.id2.drafts_folder_picker_mode", 1);

// SMTP
user_pref("mail.smtpserver.smtp1.authMethod", 3);  // 3 = 普通密码
user_pref("mail.smtpserver.smtp1.hostname", "mail.example.com");
user_pref("mail.smtpserver.smtp1.port", 587);
user_pref("mail.smtpserver.smtp1.try_ssl", 2);  // 2 = STARTTLS
user_pref("mail.smtpserver.smtp1.username", "your-email@mail.example.com");

// 自动检查
user_pref("mail.server.server2.check_new_mail", true);
user_pref("mail.server.server2.check_time", 1);
user_pref("mail.server.server2.check_all_folders_for_new", true);
```

---

*文档版本: 1.2.0 | 更新日期: 2026-10-15 | 维护者: gic-commits*
