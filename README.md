# Thunderbird ↔ Exchange 2010 原生接入桥

让 **Thunderbird（含 snap 版）** 在不依赖任何第三方插件的前提下，原生使用
企业 **Exchange 2010** 服务：

- **邮件** —— 通过本地 EWS 通道直连（TB 的 Exchange/EWS 账户）；
- **日历** —— 通过自研 **CalDAV 服务端**桥接，支持只读 + 写回（建/改/删、会议邀请）；
- **通讯录（GAL）** —— 通过自研 **只读 LDAP 服务端**桥接，供写信时自动补全。

> 设计细节见 [docs/DESIGN.md](docs/DESIGN.md)，变更记录见 [CHANGELOG.md](CHANGELOG.md)。

---

## 1. 背景与思路

Thunderbird 原生只支持 IMAP/POP3 + CalDAV/CardDAV（或插件），**不会说 EWS**，
而 Exchange 2010 主要暴露 EWS/OWA。于是本项目在**本机**充当"翻译层"：

```
Thunderbird ──(原生 CalDAV :8081)──> ewscaldav.py ──┐
Thunderbird ──(原生 LDAP   :1389)──> ldapgal.py   ──┼──> ews_bridge.py ──(NTLM)──> mail.example.com:443 EWS
Thunderbird ──(原生 IMAP/SMTP     )──────────────────┘
```

核心原则：**TB 侧零插件、只用原生协议；所有 EWS 专有知识收敛在本地桥**。

## 2. 组件

| 文件 | 端口 | 说明 |
|---|---|---|
| `ews_bridge.py` | 8080 | EWS 通道库 + 本地 IMAP/SMTP 桥：NTLM 握手（交给 `curl --ntlm`）、请求体重写、自带 DNS、EWS 操作级日志 |
| `ewscaldav.py` | 8081 | CalDAV 服务端：PROPFIND / REPORT（multiget、calendar-query）/ GET / PUT / DELETE；EWS `FindItem`+`CalendarView` → ICS；UID↔ItemId/ChangeKey 落 SQLite；写回 `CreateItem`/`UpdateItem`/`DeleteItem` |
| `ldapgal.py` | 1389 | 只读 LDAP v3（Bind/Search，BER 最小编解码）：过滤器抽取 → EWS `ResolveNames`（GAL），90s 缓存，单次 ≤50 条 |
| `check-ews-url.sh` | - | EWS 桥地址守卫：校验/恢复 TB 的 `ews_url` 指向本机桥（配 10min systemd timer） |
| `tests/` | - | 自测：EWS 联系人/ResolveNames 探测、LDAP Bind+Search smoke test |

## 3. 功能现状

### 3.1 邮件
TB 以 Exchange(EWS) 账户接入，HTTP/IMAP/SMTP 经 8080 桥透传；EWS `POST …/exchange.asmx`
走 `curl --ntlm`。请求体重写用于规避 EWS 2010 的只读写校验
（`ErrorChangeKeyRequiredForWriteOperations`）。

### 3.2 日历（读 + 写回）
- **读**：层级发现（principal → calendar-home → calendar）、`calendar-multiget`、
  `calendar-query(time-range)`、单事件 GET / 全量 GET。事件窗口 ±180 天、≤1200 条。
- **写回**：TB 建/改/删 → `PUT`/`DELETE` → EWS `CreateItem`/`UpdateItem`/`DeleteItem`。
- **会议邀请**：ICS 含 `ATTENDEE` 时发送邀请——新建 `SendToAllAndSaveCopy`、
  更新 `SendToChangedAndSaveCopy`、删除 `SendToAllAndSaveCopy`；无参会者 `SendToNone`。
- **时区**：TB 发本地墙钟（`TZID=Asia/Shanghai` 或浮点）→ 桥按 +08:00 转 UTC（`Z`）；
  UTC/全天日期原样。
- **附件**：URI 型附件（`ATTACH:http://…`）落库到正文标记，读回还原为 `ATTACH`；
  文件型附件（`ATTACH;ENCODING=BASE64`）路径已实现，但见 §5 限制。

### 3.3 通讯录（GAL 只读）
Exchange 的 `ResolveNames` 是"搜索"而非"枚举"，语义与 LDAP `SearchRequest` 对齐，
故用 LDAP 而非 CardDAV。过滤器抽取取最长搜索词 → `ResolveNames`。

## 4. 快速开始

### 4.1 配置凭据（不进源码）

```bash
mkdir -p ~/.config/ews-bridge
cat > ~/.config/ews-bridge/cred.json <<'EOF'
{
  "host": "mail.example.com",
  "port": 443,
  "sni": "mail.example.com",
  "user": "you@your-company.com",
  "password": "your-password",
  "proxy": null,
  "listen": "127.0.0.1",
  "lport": 8080,
  "log": "/tmp/ews-bridge.log",
  "ldap_port": 1389,
  "ldap_base": "dc=example,dc=com"
}
EOF
chmod 600 ~/.config/ews-bridge/cred.json
```

> `cred.json` 已在 `.gitignore` 中，请勿提交。示例见 `cred.json.example`。

### 4.2 启动服务

```bash
python3 ews_bridge.py                 # EWS 通道（8080）
python3 ewscaldav.py                  # CalDAV 日历（8081）
python3 ldapgal.py 1389               # LDAP 通讯录（1389）
```

systemd 用户单元模板见 `systemd/`（`ewscaldav.service` / `ldapgal.service` /
`check-ews-url.{service,timer}`）。

### 4.3 Thunderbird 侧配置

1. **日历** → 新建日历 → 网络日历（CalDAV），URL：
   `http://127.0.0.1:8081/dav/you@your-company.com/exchange/`
2. **地址簿** → 新建 → LDAP 目录：Host `127.0.0.1`、Port `1389`、
   Base DN `dc=example,dc=com`（勾选"写信时在地址簿中查找"）。
3. **写信自动补全** → 设置 → 撰写 → 地址：把 **Directory Server** 从 `None`
   改为该 LDAP 目录（等价：about:config 设
   `ldap_2.autoComplete.useDirectory=true` 与 `ldap_2.autoComplete.directoryServer=…`）。

### 4.4 守护 EWS 桥地址（可选）

某些情况下 TB 的 `ews_url` 会被改回真实域名而绕过本机桥，`check-ews-url.sh`
可检测并恢复：

```bash
TB_PROFILE=~/.thunderbird/<profile> ./check-ews-url.sh      # 检查/恢复
TB_PROFILE=~/.thunderbird/<profile> ./check-ews-url.sh -n   # 仅检查（dry run）
systemctl --user enable --now check-ews-url.timer           # 每 10 分钟巡检
```

## 5. 已知限制

- **日历附件的 TB 前端不完整**：Thunderbird 事件对话框的附件菜单只提供"URL"
  （`chrome/calendar/content/calendar-event-dialog.xhtml` 仅 `cmd_attach_url`，
  另有被禁用的 `cmd_attach_cloud` 占位）。因此**无法在 TB 里加本地文件附件**；
  桥端的文件型附件（base64）代码保留但实际不会被 TB 触发。
  待上游补全 UI 后即可直接接上。
- **接受邀请的重复事件**：Exchange 2010 日历项不返回 iCalendar UID，桥侧生成
  UUID；用户在邮箱里"接受"邀请时 TB 用邀请内嵌的原始 UID 建本地事件，二者不同
  → 可能显示重复。根治需"邀请邮件原始 UID 对齐"（见路线图）。
- **仅 Exchange 2010 / EWS SOAP**：依赖 `curl --ntlm`，不支持现代认证。
- **单账户、仅本机回环**：三个服务均只监听 `127.0.0.1`。
- **etag 版本**：读回序列化变化（如新增 `ATTENDEE`）时需递增 `etag()` 后缀
  以强制客户端重取一次，否则客户端按旧 etag 命中缓存。

## 6. 关键设计要点（详见 docs/DESIGN.md）

- **CalDAV 互通陷阱**：TB 的 `<D:href>` 为大写（正则须 `re.I`）；REPORT 常为
  **chunked** 编码（须实现 chunked 读取），否则永远读到空 body。
- **读写权限**：`PROPFIND` 必须宣告 `current-user-privilege-set`
  含 `write/write-content/bind/unbind`，否则 TB 每次启动把日历强制置为只读
  （且该标志"只置不清"）。
- **写回踩坑**：`CalendarItem` 子元素须按 schema 顺序；`UpdateItem` 的 FieldURI
  分命名空间（`item:*` vs `calendar:*`）；写操作强制 `ChangeKey`；
  空的 `OptionalAttendees` 会 `ErrorSchemaValidation`。
- **`FindItem` 不返回 Body/结构化参会者**：正文经批量 `GetItem(IdOnly+item:Body)`
  补齐，参会者取自本地映射库，避免逐事件 `GetItem` 拖慢同步。
- **凭据安全**：密码统一在 `~/.config/ews-bridge/cred.json`（600 权限）。

## 7. 路线图

- [x] M0 邮件桥（NTLM 透传 + 请求重写 + 操作日志）
- [x] M1 日历只读（CalDAV 发现/查询/多取）
- [x] M2 日历写回（PUT/DELETE → EWS Create/Update/Delete）
- [x] M3 会议邀请（参会者 → 邀请/更新/取消）
- [x] M4 通讯录 GAL 只读（LDAP → ResolveNames；地址簿搜索 + 写信补全）
- [x] M5 时区修复、URI 附件、日历读写权限
- [ ] M-UID 对齐：消除"接受邀请"产生的日历重复
- [ ]（上游）TB 事件对话框支持本地文件附件

## 8. 测试

```bash
python3 tests/ews_contacts_test.py   # 探测 ResolveNames 字段与服务器约束
python3 tests/ldap_smoke_test.py     # 自造 LDAP Bind+Search，验证过滤器抽取与条目返回
```

## 9. 安全与免责

- 仅限自有账号、内网/回环调试；`cred.json` 已 gitignore，请勿提交。
- 服务只监听 `127.0.0.1`，不对外暴露。
- 本项目与 Microsoft / Mozilla 无关联，仅供互操作研究。
