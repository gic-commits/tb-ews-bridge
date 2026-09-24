# Changelog

## 2026-09-24（M0 邮件桥两处修复）

### M0 修复 — 点未读邮件不变已读（写操作缺 ChangeKey）✅
- **现象**：TB 里点开未读邮件，列表不立即变已读；日志反复出现
  `ErrorChangeKeyRequiredForWriteOperations`。
- **根因**：TB 原生 EWS 发出的 `UpdateItem`（`IsRead` 置位）`ItemId` 不带
  `ChangeKey`，EWS 2010 写校验一律拒绝；此前桥只重写 `archive`/`InternetMessageId`，不补 ChangeKey。
- **修复**（`ews_bridge.py`）：`CK_CACHE` + 写操作（Update/Delete/Move/Copy）
  出站前 `ensure_changekeys` —— 缺 ChangeKey 的 `ItemId` 经批量
  `GetItem(IdOnly)`（**不带** `Traversal`，否则 `ErrorSchemaValidation`）回取并缓存注入；
  响应含 ChangeKey 类错误码时清缓存、强制刷新重试一次；成功响应
  `harvest_changekeys` 回填缓存。
- **验证**：裸 `UpdateItem`（无 CK）经桥两次均 `NoError`；真机点未读邮件立即变已读。

### M0 修复 — 转发带图邮件收件人看不到图片 ✅
- **现象**：转发含内联截图的邮件，收件人看不到图片（自己发件箱能看）。
- **根因**：TB 把 `cid:` 引用改写成私有
  `x-moz-ews://user@host/…?part=1.2.N&type=…&filename=…` 直接塞进出站
  `MimeContent`（仅 ~4.5KB，不含图片数据），出站前未还原为 `cid:`+附件；
  收件人客户端无法解析该私有 URL。
- **修复**：桥维护 filename→(ctype, data) 的 MIME 附件缓存——凡 EWS 响应
  （`GetItem` 含 `MimeContent`、`FileAttachment` 含 `Content`）路过即
  `harvest_mime` 落缓存（LRU，64 条/96MB）；`CreateItem` 出站前
  `fix_forward_images` 解码 `MimeContent`，把 `x-moz-ews://` 按
  `filename=` 查缓存改写为 `cid:`，缺图打 `IMGFIX-MISS` 日志并保持原样
  （与修复前行为一致，不引入回归），命中的图片部件以 base64 +
  `Content-ID` 重新挂进 `multipart/related`（顶层非 related 时包一层，
  Subject/To 等头移到外层），重编码回 `MimeContent` 并同步 `Content-Length`。
- **诊断**：`ews_via_curl` 补 `CURL-RC`（curl 非零退出）与 `TRUNC`
  （响应 `Content-Length` ≠ 实收）日志，防大响应静默截断。
- **验证**：离线用真实转发 dump + 合成缓存单测通过；真机经桥
  `GetItem(IncludeMimeContent)` 暖缓存 → 重放 `SaveOnly` 建草稿 →
  取回草稿 `MimeContent` 确认：`multipart/related` >
  `multipart/alternative` + 3 个有效 PNG（`Content-ID` 与 HTML
  `src="cid:…"` 全部对应，quoted-printable 解码后 0 处 `x-moz-ews`）→
  草稿硬删除清理。

## 2026-09-22 ~ 09-23（M0 邮件桥 + M1 日历只读 + M4 LDAP 起步）

### M0 — 邮件桥（`ews_bridge.py`，17080）
- TCP 级 IMAP/SMTP/HTTP 透传；EWS POST 交 `curl --ntlm` 完成 NTLM 握手。
- 请求体重写：`Id="archive"`→`inbox`、剔除 `InternetMessageId`，
  规避 EWS 2010 SP3 只读写校验（`ErrorChangeKeyRequiredForWriteOperations`）。
- 操作级日志：SOAP 操作名 + Fault/ResponseCode 单行摘要。
- 自带 A 记录 DNS 查询 + 5min 缓存，`--resolve` 钉 IP。

### M1 — CalDAV 日历只读（`ewscaldav.py`，17081）✅ 可用
- 发现层：OPTIONS / PROPFIND（principal → home → calendar，Depth:1 列事件）。
- 查询层：REPORT `calendar-multiget` 与 `calendar-query(time-range)`；
  GET 单事件 / 全量 ICS。
- EWS 数据源：`FindItem + CalendarView`（±180d，≤1200，5 次重试）。
- **修复**：multiget href 正则大小写（TB 发大写 `<D:href>`）；
  `_body()` 支持 chunked；`_logreq()` 只读一次并返回 body
  （曾因 Content-Length 门槛 + 双重读取掩盖报文 → 日历空显示）。
- UID 映射：SQLite `uid ↔ ItemId/ChangeKey`（2010 不提供 GlobalObjectId）。
- 真机：TB156 订阅成功，depth-1 列 159 条、multiget 取回 135 条事件。

### M4 — 只读 LDAP 通讯录（`ldapgal.py`，17089）✅ 桥端到端已通
- LDAP v3 Bind/Search/Unbind 最小子集；BER 编解码；SearchResEntry + Done。
- 过滤器抽取：and/or/not 递归、子串/等值叶子解析（递归下降嵌套结构），
  最长搜索词 → EWS `ResolveNames`。
- 属性映射（cn/sn/givenname/mail/title/department/company/mobile/l/objectclass）；
  90s 结果缓存；单次 ≤50 条；EWS 重试 3 次。
- 实测：`ResolveNames` 不接受 `SearchScope`（去掉即正常）；
  `ReturnFullContactData=true` 字段齐全；个人 Contacts 文件夹为空 →
  放弃 CardDAV 路线，选 LDAP。
- **修复**：
  - `filt` 由裸 bytes 改为 `(tag, value)` 元组传入 `extract_term`；
  - Bind 版本号取值纠正（曾把 tag 当版本，现取 `pv[2]`）；
  - `extract_term` 叶子 `terms.append(t)` 曾误存 `(attr,val)` 元组
    （`t.strip()` 崩）→ 改取 `t[1]`；
  - `_load_bridge` `mod.LOG=None` 导致 EWS 日志 `NoneType.write`
    → 改打开真实文件句柄；
  - `_send` 顶层 `enc(0x30, seq0(midb, op))` 多包一层 SEQUENCE（TB 无法解析）
    → 改 `enc(0x30, midb + op)`。
- 联调：自测客户端 `enc()` 长长度分支漏拼 payload 导致 3 字节假包
  （客户端 bug，服务端行为正确），修正后 131B 完整报文到达。
- ⭐ 端到端：`tests/ldap_smoke_test.py` → `bind: OK`、
  `(cn=*li*)` 返回 15 条 SearchResultEntry + Done(success)。
- 归档副本实测 `_gal_search('li')` → 15 人可用；测试文件改为从
  `cred.json` 读凭据（去除明文密码）。
- ⭐ TB 实测通过（白天，运行目录修复后同步归档）：
  - 响应 protocolOp 改 **implicit tagging**：`61/64/65/78` 直接包字段，
    去掉多余内层 SEQUENCE（原多包一层 `30` → TB `LDAPMessage.sys.mjs`
    asn1js `value[1] undefined` 崩溃，bind 后永不发搜索）。
  - SearchRequest 过滤器 tag 修正：context-specific 构造标签
    `0xa0/0xa1/0xa2/0xa4/0xa3…`（原误判 `0x80/0x81…` → `term=''`）。
  - TB 地址簿搜索 `li` 正常显示联系人；日志链路
    `bind LDAPv3 → search term='li' → 15 Entry → unbind`；
    三轮响应均过 TB 自带 `LDAPMessage.sys.mjs fromBER/parse` 验证。
- ⭐ 写信收件人自动补全：TB 默认不查 LDAP 目录，需在
  **设置 → 撰写 → 地址 → Directory Server** 从 `None` 改为"Directory"
  （或 about:config 设 `ldap_2.autoComplete.useDirectory=true` +
  `ldap_2.autoComplete.directoryServer=ldap_2.servers._nonascii`）。
  设置后英文 `li`、中文 `李` 均可带出联系人（08:00 实测）。

### M2 — 日历写回（EWS CreateItem/UpdateItem/DeleteItem）✅ 全链路通过
- 方向确认（owl xpi 参考）：CreateItem 用 `SendMeetingInvitations`；
  UpdateItem 用 `MessageDisposition="SaveOnly"` +
  `SendMeetingInvitationsOrCancellations`；DeleteItem 用
  `DeleteType="HardDelete"`；Exchange2010 无 `SuppressReadReceipts`。
- 实现：`do_PUT`（新建 201 / 更新 204）与 `do_DELETE`（204）替代 501；
  `parse_ics` 抽取 UID/SUMMARY/LOCATION/DTSTART/DTEND/DESCRIPTION，
  时间归一化为 ISO `Z`；UID↔ItemId/ChangeKey 落 SQLite。
- **修复 1 — CreateItem 元素顺序**：`ErrorSchemaValidation`。
  `CalendarItem` 子元素须按 schema 顺序：`Subject → Body → Start → End
  → Location`（原 Body 放最后被拒）。
- **修复 2 — UpdateItem FieldURI 命名空间**：`ErrorSchemaValidation`
  "枚举约束失败"。"日历子元素命名空间剥离"：`Subject`/`Body` 属通用
  **item** 属性（`item:Subject`、`item:Body`），`Start/End/Location`
  属 **calendar** 属性（`calendar:*`），两者不可混用 `calendar:*`。
- **修复 3 — 写操作需要 ChangeKey**：`ErrorChangeKeyRequiredForWriteOperations`。
  EWS 2010 写操作（Update/Delete）强制带 `ChangeKey`；本地无缓存时经
  `_fetch_changekey` re-FindItem 回取最新 ChangeKey 再重试。
- 失败判定升级：仅看 200 + 无 Fault 不够，须解析 `ResponseClass`
  （UpdateItem 可 200 而 `<UpdateItemResponseMessage ResponseClass="Error">`）。
- ⭐ 真机全链路（经 17081 桥）：`PUT` 建（201）→ `PUT` 改（204，
  主题/时间/地点全变）→ `DELETE` 删（204），EWS FindItem 确认无残留。
- 测试卫生：真实日历破坏性测试统一 `M2-TEST-*` 前缀并事后自清理。

### M3 时区修复 — CalDAV 写回浮点时间偏移（✅ 已修）
- **现象**：TB 在 Asia/Shanghai 下新建事件，桥端按 UTC 原样存储 → 事件
  在 Exchange/日历上整体偏移 +8 小时（本地 13:00 被存成 13:00Z）。
- **根因**：`parse_ics.norm()` 把无 `Z` 结尾的浮点时间当 UTC；而 TB 发
  `DTSTART;TZID=Asia/Shanghai:...`（本地墙钟时间）或纯浮点（本地）。
- **修复**：无 `Z` 时按本地时区 `Asia/Shanghai(+08:00)` 转成 UTC 再回写
  `Z`；带 `TZID=Asia/Shanghai` 同样按本地偏移处理；UTC(`Z`) 与全天日期
  （纯 8 位）原样保留。读取侧 `ews_find` 返回 UTC → TB 自动转本地显示。
- **验证**：TB 新建"再建一个会议测试"（本地 9/24 13:00）→ EWS 存
  `2026-09-24T05:00:00Z`（=北京 13:00）✓；`IsMeeting=true`, Sent=true，
  参会者齐全 ✓。上一"测试新建"（偏移事件）已由用户在 TB 删除。

### M3 — 会议邀请（EWS CreateItem/UpdateItem/DeleteItem + 参会者）✅ 全链路通过
- `parse_ics` 解析参会者：`ATTENDEE;ROLE=REQ-PARTICIPANT` → 必选、
  `ROLE=OPT-PARTICIPANT` / `OPTIONAL-ATTENDEE:` → 可选；支持多参数、
  续行折叠、`mailto:` 前缀剥离。
- 发送策略（对齐 owl）：**新建**有参会者 → `SendMeetingInvitations=
  SendToAllAndSaveCopy`（否则 `SendToNone`）；**更新** → `SendMeetingInvitationsOrCancellations=
  SendToChangedAndSaveCopy`（仅通知变更）；**删除**有参会者 →
  `SendMeetingCancellations=SendToAllAndSaveCopy`，无参会者 → `SendToNone`。
- EWS 2010 schema 顺序补充：`RequiredAttendees/OptionalAttendees` 位于
  `Location` 之后；`UpdateItem` 用 `FieldURI=calendar:RequiredAttendees|OptionalAttendees`。
- DB 表 `ev` 增列 `attendees`（JSON），供 DELETE 判断是否发取消通知。
- ⭐ 真机验证：建会 `IsMeeting=true`、`MeetingRequestWasSent=true`、
  `MyResponseType=Organizer`，GetItem 参会者字段齐全；经 17081 桥
  `PUT 201（含参会者）→ DB 记录 attendees → DELETE 204`、EWS 无残留。

### M5 — 会议附件（URI 型）✅ 全链路通过
- 背景：TB156 日历的附件页签/富文本只接受 **URL**（`http(s)://…`）或本地
  路径链接，**不会**把本地文件经 CalDAV 上传；EWS 又没有"URI 附件"对象，
  故 URI 附件唯一可行落点是正文。
- `parse_ics` 解析 `ATTACH`：`ENCODING=BASE64/VALUE=BINARY` → 文件附件；
  其余形如 `scheme:…` → URI 附件；`_split_ic` 取**第一个引号外冒号**切分
  参数/值（`ALTREP="data:text/html,…"` 内含冒号），`prop/uid/attendee`
  改用 `unfolded` 行 → 修复含 ALTREP 的 DESCRIPTION 导正文本丢失。
- 写侧：URI 附件以纯文本行 `附件: <url>` 并入 `Body`（`_body_with_uris`），
  幂等——已存在则不重复追加；`BodyType` 由 `HTML` 改 `Text`
  （EWS `<t:Body>` 是纯文本内容模型，塞 `<br/>` 片段会
  `ErrorSchemaValidation`），并对 subject/location/body 做 XML 转义。
- 读侧：**FindItem 永不返回 Body**，新增 `ews_bodies_batch`（分块 50/次
  `GetItem` IdOnly+`item:Body`）供列表批量取正文；`to_ics` 将
  `附件: <url>` 还原为 `ATTACH:<url>` 行并从 DESCRIPTION 剔除，避免重复。
  单事件 GET / multiget 才走 `_lazy_item_full`（AllProperties）拉附件。
- 性能：修正初版"每事件一次 GetItem + 拉全部内联图片 base64"导致全量
  列表 24s / 18MB → 现 **2.8s 冷启 / 0.32s 缓存 / 219KB**，且跳过
  `IsInline=true` 的内联图片。
- ⭐ 真机验证：`PUT` 建（201，含 `ATTACH:http://www.baidu.com/`）→
  列表与单事件均读出 `DESCRIPTION:正文文本` + `ATTACH:http://…`；重复
  `PUT`（204）不累积；模拟 TB 重存（正文无标记、仅 ATTACH）URL 不丢。
  测试事件事后清理，列表恢复 161 条。

### M5 修复 — CalDAV 日历每次启动被强制只读 ✅
- **现象**：TB 重启后 Exchange 日历总是只读；用户手工在 UI 取消只读，
  重启又被覆盖（`prefs.js` 的 `calendar.registry.<id>.readOnly=true`）。
- **根因**：桥端 `PROPFIND` 的 `current-user-privilege-set` 只宣告
  `<d:read/>`；Lightning 每次启动据此判定无写权限 → 强制置 `readOnly=true`
  并写回 prefs，覆盖手工设置。
- **修复**：日历集合（`_CAL_PROPS_BASIC`）与 home 发现条目均补宣告
  `<d:write/> <d:write-content/> <d:write-properties/> <d:bind/> <d:unbind/>`。
  桥端 PUT/DELETE 早已实现（M2/M3），此前只是权限未对外声明。
- **验证**：`PROPFIND` 返回含全部写权限（curl 实测）；
  重启 TB 后 `readOnly` 应保持 false。

### M5 修复 — 带参会者的事件保存 500（`_split_ic` 闭包）✅
- **现象**：TB 保存含 `ATTENDEE` 的事件返回 500；日志
  `NameError: free variable '_split_ic' referenced before assignment`。
- **根因**：`_split_ic` 定义在 `parse_ics` 内、且在 `attendee_list()` 调用
  **之后**；Python 将其视为 `parse_ics` 局部变量，调用早于赋值 → 崩溃。
  之前本地测试的事件无参会者，故未触发。
- **修复**：`_split_ic` 提升到模块级；并删除重复的 `prop_full` 定义
  （后一个误用未展开的 `v`，折行 `DTSTART` 会解析失败）。
- **验证**：对真实失败报文 `parse_ics` → 参会者（2 人）/URI 附件/正文
  均正确；服务重启后生效。

### M5 修复 — 更新会议时 UpdateItem 500（空 OptionalAttendees）✅
- **现象**：已存在的会议加参会者后保存，`UpdateItem` 返回 500
  `ErrorSchemaValidation`。
- **根因**：`ews_update` 只要事件有参会者就同时输出
  `RequiredAttendees` 与 `OptionalAttendees` 两个 SetItemField；当可选
  参会者为空时输出 `<t:OptionalAttendees></t:OptionalAttendees>`，EWS
  要求该元素内容非空（"内容不完整，应为 Attendee 列表"）→ 校验失败。
  `CreateItem` 的 `attendees_xml` 本就按需输出，故新建不受影响。
- **修复**：`ews_update` 分别按 `required`/`optional` 是否非空决定是否输出
  对应 SetItemField。
- **验证**：对真实失败事件（仅有必选参会者）UpdateItem 返回
  `ResponseClass=Success`；随后 TB 新建带 2 名参会者的会议
  `IsMeeting=true`、邀请已发、参会者入库。

### M5 修复 — 同步性能回退（multiget 逐事件 GetItem）✅
- **现象**：`calendar-multiget` 一次 100 事件的 REPORT 达 **12.5s / 11.5MB**
  （base64 附件被内联），影响日历正常同步。
- **根因**：multiget 分支调用了 `to_ics(uid, it, full=True)`，对每个事件走
  `GetItem(AllProperties)` 并拉取附件内容。
- **修复**：multiget 改回缓存正文路径（`build_event_list` 已批量取正文，
  含 `DESCRIPTION` 与 URI 附件标记）；`full=True` 仅保留给单事件 GET。
- **验证**：100 事件 multiget → **1.0s / 161KB**；含 URI 附件的事件仍输出
  `DESCRIPTION` + `ATTACH:http://…`；单事件 GET 附件路径正常。

### M3 修复 — 读取侧不返回参会者（TB 看不到参会人）✅
- **现象**：给会议加参会者后保存，Exchange 侧已成功（`IsMeeting=true`、
  参会者齐全、邀请已发），但 TB 界面看不到参会人，误以为保存失败。
- **根因**：`to_ics` 未输出 `ORGANIZER`/`ATTENDEE`；且 `FindItem` 只返回
  `DisplayTo` 展示串、不返回结构化参会者（`FindItem` 也不含 Body）。
- **修复**：`build_event_list` 从本地 `ev.attendees`（写回时已落库）取
  参会者挂到事件上；`to_ics` 对会议输出 `ORGANIZER` 与
  `ATTENDEE;ROLE=REQ/OPT-PARTICIPANT`。读取零额外 `GetItem` 开销。
- **验证**：`test3` 单事件 GET 与全量列表均输出 3 名必选参会者；
  读回内容再 `parse_ics` 往返一致（3 人 + URI 附件 + 正文）。
- **配套**：TB 客户端按 etag 缓存，仅读回序列化变化时 etag 不变 → 客户端
  不会重取。故给 `etag()` 加读版本后缀 `-r2`，强制客户端全量重取一次；
  之后 etag 稳定，只有事件真正改动（ChangeKey 变化）才重取。

### 运维防护 — EWS 桥地址守卫（`check-ews-url.sh`）✅ 已验证
- 背景：`ews_url` 无法在 TB UI 配置，若被改回真实域名会绕过本机 EWS 桥。
- 功能：检测 `mail.server.server2.ews_url` 与
  `mail.outgoingserver.ews1.ews_url` 是否为
  `http://127.0.0.1:17080/ews/exchange.asmx`，异常则自动恢复并备份
  （`prefs.js.bak-checkews`）；`-n` 提供 dry-run 检查模式。
- 破坏性实测：伪装 ews_url 被改坏 → 脚本成功恢复两处、备份生成、
  与原始 prefs.js `diff` 完全一致。
- 配套 systemd 单元：`check-ews-url.{service,timer}` 每 10 分钟巡检
  （timer 已 user enable，service 实测 `status=0/SUCCESS`）。
- 属不属于桥本身：仅守护配置，不参与 EWS 流量（无凭据、只读 prefs）。

### 凭据
- 密码移出源码 → `~/.config/ews-bridge/cred.json`（600，已 gitignore）。
