# 设计文档：Thunderbird ↔ Exchange 2010 原生接入

> 对应实现：`ews_bridge.py` / `ewscaldav.py` / `ldapgal.py`

## 1. 总体架构

```
┌─────────────┐   IMAP/SMTP(NTLM)   ┌──────────────┐   EWS SOAP(NTLM)   ┌──────────────────┐
│ Thunderbird │ ─────────────────── │ 8080 邮件桥  │ ──────────────────▶│ mail.example.com:│
│             │   CalDAV :8081      └──────────────┘   (curl --ntlm)    │ 443 Exchange 2010│
│             │ ───────────────────▶ ewscaldav.py ─┐                    └──────────────────┘
│             │   LDAP   :1389                     │ ews_bridge.py
│             │ ───────────────────▶ ldapgal.py ───┘ (共享 EWS 通道/日志/凭据)
└─────────────┘
```

设计原则：**TB 侧零插件、只用原生协议**；所有 EWS 专有知识收敛在本地桥。

## 2. `ews_bridge.py` — EWS 通道与邮件桥

职责：

1. **邮件透传（TCP 级）**：监听 8080，客户端连入后按请求头/体转发到上游，
   EWS `POST …/exchange.asmx` 走 `curl --ntlm`（NTLM 握手交给 curl 完成）；
   其余 HTTP 转 TLS 直连。
2. **请求体重写**：正则将 `Id="archive"` → `Id="inbox"`、剔除
   `InternetMessageId` 元素，规避服务端 `ErrorChangeKeyRequiredForWriteOperations`
   一类只读校验（实测 EWS 2010 SP3 行为）。
3. **操作级日志**：从请求体抓 SOAP 操作名（`FindItem`/`GetItem`/`CreateItem`…），
   从响应抓 `Fault`/`ResponseCode`，单行 `-> code op=… [OK|FAULT|ErrorRC]`。
4. **凭据**：`load_conf()` 读 `~/.config/ews-bridge/cred.json`；
   命令行参数 > 环境变量 `EWS_CRED` > 默认路径。
5. **DNS**：自带极简 A 记录查询（绕过系统解析，配合 `--resolve` 钉 IP），
   5 分钟缓存；上游断连自动重连一次。

## 3. `ewscaldav.py` — CalDAV 日历服务（读 + 写）

### 3.1 为什么是"服务端"而不是"客户端"

Thunderbird 的原生 EWS 支持**只覆盖邮箱**，日历侧没有 EWS provider——它只会
**作为 CalDAV 客户端**拉/推日历。因此本地必须扮演一个最小 CalDAV 服务器，把 TB
的标准 WebDAV/CalDAV 报文翻译成 EWS。（邮箱侧则相反：TB 直接说 EWS，经本机桥做
NTLM 与兼容重写即可。）

### 3.2 读路径

1. `OPTIONS` → 声明 `DAV:1,2` 与允许方法。
2. `PROPFIND Depth:0/1` →
   - 层级发现：`/dav/` → principal → calendar-home → `/dav/<user>/exchange/`；
   - `Depth:1` 时列出事件 href+etag（触发 TB 进入 multiget 流程），
     清单来自 `EWS FindItem + CalendarView`（±180 天、≤1200 条、5 次重试）；
   - `getctag` 用时间戳（内容稳定即可）。
3. `REPORT calendar-multiget` → 按 href 中的 uid 取缓存 ICS。
   - **大小写陷阱**：TB 用 `<D:href>`，正则须 `re.I`。
   - **chunked 陷阱**：TB 常以 chunked 发 REPORT，`_body()` 须实现 chunked 解码；
     `_logreq()` 只读一次并返回 body。
4. `REPORT calendar-query`（time-range）→ 同样走 CalendarView。
5. `GET <uid>.ics` → 单事件；`GET` 日历集合 → 全量 ICS 拼接。
6. **权限宣告**：`current-user-privilege-set` 必须含
   `write / write-content / write-properties / bind / unbind`，否则 Lightning
   每次启动把日历置为只读（且其逻辑"只置 true、不清 false"）。

### 3.3 写路径（M2/M3）

`PUT`/`DELETE` 不再 `501`，转 EWS：

- 新建 → `CreateItem`；更新 → `UpdateItem`；删除 → `DeleteItem`。
- **schema 顺序**：`CalendarItem` 子元素须按 `Subject → Body → Attachments →
  Start → End → Location → Required/OptionalAttendees`。
- **FieldURI 命名空间**：`item:Subject`/`item:Body` 与
  `calendar:Start/End/Location/RequiredAttendees/…` 不可混用。
- **ChangeKey 强制**：EWS 2010 写操作必须带 `ChangeKey`；本地缺失时
  `_fetch_changekey` re-`FindItem` 回取。
- **发送策略**：有参会者时新建 `SendToAllAndSaveCopy`、更新
  `SendToChangedAndSaveCopy`、删除 `SendToAllAndSaveCopy`；无参会者 `SendToNone`。
  空的 `OptionalAttendees` 会 `ErrorSchemaValidation`，故按需输出。
- **失败判定**：不能只看 HTTP 200，须解析 `ResponseClass`。

### 3.4 EWS UID 映射

Exchange 2010 不返回日历事件的 iCalendar UID。策略：

- SQLite（`~/.cache/ewscaldav.db`）表 `ev(uid, itemid UNIQUE, changekey, attendees)`；
- 首见某 `ItemId` → 生成 `uuid4()` 作为 CalDAV 侧 UID；
- 再见则复用并更新 `changekey`（作 etag）。

**已知问题**：用户在 TB 中"接受"会议邀请时，TB 用邀请内嵌 VCALENDAR 的原始
UID 建本地事件，与桥生成的 UUID 不同 → **同一会议显示两次**。根治方案
（M-UID）：从邀请邮件解析原始 UID 并映射。

### 3.5 `FindItem` 的能力边界

`FindItem` **不返回 Body**，也只返回 `DisplayTo` 展示串（无结构化参会者）。
因此：

- **正文**：批量 `GetItem`（`IdOnly` + `AdditionalProperties item:Body`）补齐，
  结果按 `itemid` 缓存、按 `ChangeKey` 失效；列表序列化不再逐事件 `GetItem`。
- **参会者**：写回时已存本地 `ev.attendees`，读回时直接输出
  `ORGANIZER`/`ATTENDEE`，零额外 EWS 调用。
- **附件**：单事件 GET 才走 `GetItem(AllProperties)` 拉取，并跳过
  `IsInline` 的内联图片，避免同步体积膨胀。

### 3.6 时区与附件

- **时区**：无 `Z`/带 `TZID=Asia/Shanghai` 的浮点时间按 +08:00 转 UTC（`Z`）；
  UTC 与全天纯 8 位日期原样保留。
- **URI 附件**：EWS 无"URL 附件"对象，故并入正文纯文本行 `附件: <url>`
  （`<t:Body>` 是纯文本内容模型，塞 HTML 片段会校验失败）；读回时把该行
  还原为 `ATTACH:<url>` 并从 `DESCRIPTION` 剔除，且幂等去重。
- **文件附件**：`ATTACH;ENCODING=BASE64` 解析与 `CreateAttachment` 路径已实现，
  但 TB 当前 UI 不会产生（见 §5）。
- **etag 版本**：读回序列化变化时递增 `etag()` 后缀（如 `-r2`），
  令客户端重取一次；之后仅 `ChangeKey` 变化才重取。

## 4. `ldapgal.py` — 只读 LDAP 通讯录（M4）

### 4.1 为什么 LDAP 而不是 CardDAV

个人 Contacts 文件夹为空；而 TB **写信自动补全**依赖"可搜索目录"。
Exchange `ResolveNames` 是搜索接口、不可枚举，无法支撑 CardDAV 的全量同步语义；
LDAP `SearchRequest` 恰好也是"带过滤器的搜索"，语义对齐，且 TB 原生支持。

### 4.2 协议实现（只读最小子集）

```
BindRequest    (0x60) → BindResponse success (0x61)
SearchRequest  (0x63) → SearchResEntry* (0x64) + SearchResultDone (0x65)
UnbindRequest  (0x42) → 关闭
ExtendedRequest(0x77, 如 StartTLS) → protocolError（本机回环，无需 TLS）
Abandon/Cancel (0x75/0x76) → 忽略
```

BER 编解码：`enc/read_tlv/walk_elems` 只支持短/长长度与基本 TLV。

### 4.3 过滤器抽取

SearchRequest 的 Filter 是 BER CHOICE：

- `and/or (0xa0/0xa1)`：递归子过滤器；
- `not (0xa2)`：取子过滤器；
- `equality/substr/ge/le/approx (0xa3/0xa4/…)`：叶子，调 `av()` 解析
  AttributeDescription + AssertionValue（子串嵌套 initial/any/final）；
- `present (0x87)`：仅记录。

收集到的搜索词**按长度降序取最长**，交给 `ResolveNames(UnresolvedEntry)`。

### 4.4 属性映射与结果编码

| LDAP 属性 | EWS 字段 |
|---|---|
| cn/displayname/name | DisplayName（回退 Name） |
| sn / givenname | Surname / GivenName |
| mail | EmailAddress |
| title / department / company | JobTitle / Department / CompanyName |
| mobile / telephonenumber | PhoneNumbers Entry Key=MobilePhone |
| l | PhysicalAddresses/Business → City |
| objectclass | top,person,organizationalperson,inetorgperson |

条目 DN：`cn=<displayname>,dc=example,dc=com`（displayname 做 RDN 转义）。

### 4.5 服务器实测约束

- Exchange 2010 SP3 的 `ResolveNames` **拒绝 `SearchScope` 属性**
  （`ErrorSchemaValidation`），去掉即正常；
- `ReturnFullContactData=true` 返回完整字段；
- 多命中时 `ErrorNameResolutionMultipleResults` 是**预期**错误码；
- 无照片接口（2010 无 `GetUserPhoto`）。

### 4.6 缓存与限流

- 查询结果缓存 90s，上限 512 条（满则清空）；
- 单次搜索最多返回 50 人；
- EWS 调用失败重试 3 次（间隔 0.6s）。

## 5. 附件：TB 前端不完整

Thunderbird 的事件对话框**只提供 URL 附件**：`chrome/calendar/content/
calendar-event-dialog.xhtml` 中仅有 `cmd_attach_url`，以及被禁用的
`cmd_attach_cloud` 占位，没有"本地文件"入口。因此：

- 桥端文件型附件（base64 → `FileAttachment`）代码保留，但 TB 不会触发；
- URI 附件是当前唯一可用路径，落库到正文并在读回时还原为 `ATTACH`。

待上游补全前端（`cmd_attach_cloud` 暗示预留扩展点）后，桥端 base64 路径可
直接接上，基本无需改动。

## 6. 凭据与安全

- 密码统一 `~/.config/ews-bridge/cred.json`（`chmod 600`），源码/仓库只保留
  `load_conf()` 加载逻辑；环境变量 `EWS_CRED` 可覆盖路径。
- 三个服务均只绑定 `127.0.0.1`。
- 日志 `/tmp/ews-bridge.log`，前缀 `CALD`/`LDGAL` 区分组件。

## 7. 测试策略

1. `tests/ews_contacts_test.py`：直连 EWS 探测 contacts 文件夹、
   `ResolveNames` 字段/属性约束。
2. `tests/ldap_smoke_test.py`：不经 TB，自造 LDAP Bind+Search
   （substring 过滤器 `(cn=*li*)`），验证 BER 解析、过滤器抽取、条目编码、
   `SearchResultDone` 全链路。
3. 真机验证：TB 订阅 CalDAV → 核对事件数；TB LDAP 地址簿写信补全；
   日历建/改/删与会议邀请。

## 8. 路线图

- **M-UID 对齐**：邀请邮件 MIME/VCALENDAR 解析原始 UID → 映射表
  `uid ↔ itemid`，桥侧 ICS 输出原始 UID，消灭"接受邀请"重复。
- **文件附件**：待 TB 事件对话框支持本地文件后，接通桥端 base64 路径。
- **CardDAV**：待个人 Contacts 文件夹有数据后再评估。
