# 部署与使用指南（从零开始）

> English: [SETUP.md](SETUP.md)

本文按顺序描述：安装 Thunderbird → 部署本地桥 → 建立 Exchange(EWS) 账户 →
指向本机桥 → 加日历/通讯录 → 常驻与守护。

## 0. 前置条件

- Linux 桌面（本项目在 Kylin/ARM(aarch64) 上验证，x86_64 同样适用）。
- **Python 3.8+**（仅用标准库，无需 pip 依赖）与 **`curl`**。
- 一个 **Exchange 2010** 账号（邮箱 + 密码），且 EWS 可用：
  - `https://<exchange-host>/EWS/Exchange.asmx` 可访问（未认证会返回 401）；
  - 浏览器能登录 `https://<exchange-host>/owa/`。
- 能解析内网 DNS（`<exchange-host>` 指向内网地址）。

## 1. 安装 Thunderbird

- snap：`sudo snap install thunderbird`
- 或发行版仓库：`sudo apt install thunderbird`
- 或从官网下载安装包。

**版本要求**：原生 Exchange(EWS) **邮箱**支持。判断方法：新建账户时可见
“Exchange”类型；或 `about:config` 搜索 `ews` 有相关项。旧版 TB 没有该支持。

> 注意：原生 EWS 只覆盖**邮箱**；日历/通讯录没有 EWS provider，所以本项目分别
> 用 CalDAV / LDAP 桥接。

## 2. 部署本地桥

```bash
git clone https://github.com/<you>/tb-ews-bridge.git
cd tb-ews-bridge

mkdir -p ~/bin ~/.config/ews-bridge
cp ews_bridge.py ewscaldav.py ldapgal.py check-ews-url.sh ~/bin/
chmod +x ~/bin/check-ews-url.sh

cp cred.json.example ~/.config/ews-bridge/cred.json
$EDITOR ~/.config/ews-bridge/cred.json      # 见下表填真实值
chmod 600 ~/.config/ews-bridge/cred.json
```

`cred.json` 字段：

| 字段 | 说明 |
|---|---|
| `host` / `port` / `sni` | Exchange 主机名、443、SNI（通常同 host） |
| `user` / `password` | 域名账号（UPN，如 `you@your-company.com`）与密码 |
| `lport` | 邮件 EWS 桥本地端口，默认 `17080` |
| `ldap_port` | LDAP 桥端口，默认 `17089` |
| `ldap_base` | GAL 的 Base DN，如 `dc=example,dc=com` |
| `listen` / `log` | 绑定地址（`127.0.0.1`）/ 日志文件 |

## 3. 启动并自检

```bash
python3 ~/bin/ews_bridge.py      # 邮件 EWS 中继 17080
python3 ~/bin/ewscaldav.py       # CalDAV 日历    17081
python3 ~/bin/ldapgal.py         # LDAP 通讯录    17089
```

自检（另开终端）：

```bash
ss -ltn | grep -E '17080|17081|17089'          # 三个端口在监听
curl -s -o /dev/null -w '%{http_code}\n' -X PROPFIND \
  -H 'Depth: 0' --data '<D:propfind xmlns:D="DAV:"><D:prop><D:resourcetype/></D:prop></D:propfind>' \
  http://127.0.0.1:17081/dav/you@your-company.com/exchange/   # 期望 207
```

## 4. Thunderbird：建立 Exchange(EWS) 邮箱账户

1. **账户设置 → 新建 → 邮箱**，输入姓名、邮箱地址、密码，继续。
2. TB 会尝试 **Autodiscover**。对老 Exchange 2010 可能失败或指向真实域名；
   若失败，改用**手动设置**：
   - 账户类型选 **Exchange (EWS)**；
   - 服务器填真实 `host`，用户名填 UPN，端口 443 + SSL。
3. **关键一步——把 `ews_url` 指向本地桥**（该项**不在 UI**，只能改配置）：
   - **完全退出 TB**（确认 `pgrep thunderbird` 无输出）；
   - 编辑 `~/.thunderbird/<profile>/prefs.js`（<profile> 如 `xxxx.default-release`）：
     - `mail.server.serverN.ews_url` → `http://127.0.0.1:17080/ews/exchange.asmx`
     - `mail.outgoingserver.ewsN.ews_url` → 同上
   - 重新打开 TB，能收发即成功。
4. 用 `<repo>/systemd/` 里的 `check-ews-url.timer` 守护该值，防止被改回真实域名
   （见 §7）。

> 本地桥对上游做 **NTLM 握手 + 请求体重写**，以适配 EWS 2010；TB 侧仍是它自己的
> 原生 EWS。

## 5. 添加 CalDAV 日历

1. **日历 → 新建日历 → 网络日历（CalDAV）**，URL：
   `http://127.0.0.1:17081/dav/you@your-company.com/exchange/`
2. 订阅后即可浏览/编辑事件；若显示为“只读”，见 §9。

## 6. 添加 LDAP 通讯录与写信补全

1. **地址簿 → 新建 → LDAP 目录**：
   - Host `127.0.0.1`，Port `17089`，Base DN `dc=example,dc=com`；
   - 勾选“写信时在地址簿中查找”。
2. **写信自动补全**：设置 → 撰写 → 地址，把 **Directory Server** 从 `None`
   改为该 LDAP 目录（等价于 about:config 设
   `ldap_2.autoComplete.useDirectory=true` 与
   `ldap_2.autoComplete.directoryServer=ldap_2.servers.<name>`）。

## 7. 安装为常驻服务（systemd --user，开机自启）

```bash
mkdir -p ~/.config/systemd/user
cp systemd/*.service systemd/*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ews-bridge.service ewscaldav.service ldapgal.service
systemctl --user enable --now check-ews-url.timer     # 每 10 分钟守护 ews_url
```

> 单元模板用 `%h/bin/...`，对应 §2 的 `~/bin`。改过端口的话，需在
> `ewscaldav.service`/`ldapgal.service` 的 `ExecStart` 里加端口参数
> （邮件端口改 `cred.json` 的 `lport`）。

## 8. 验证清单

- `ss -ltn` 三个端口在监听；
- TB **收发邮件**正常；
- TB **日历**能新建/编辑事件（非只读）；
- TB **地址簿**能搜索到人、写信能自动补全。

## 9. 常见问题

- **邮件 401 / 认证失败**：多为域账号被锁或密码过期。先用浏览器登录
  `https://<exchange-host>/owa/` 验证；账号被锁通常约 30 分钟自动解锁。
- **日历总是只读**：确认桥的 `PROPFIND` 返回 `current-user-privilege-set`
  含 `write`（本仓库已支持）；TB 只在 `readOnly=false` 时才重估权限。
- **会议附件只能填 URL**：TB 事件对话框前端限制（无本地文件入口），桥端
  文件附件（base64）已实现但不会被触发。
- **接受邀请出现重复事件**：Exchange 2010 不通过标准字段给出 iCalendar UID；
  见 README「已知限制」的 M-UID 说明。
- **端口冲突**：改端口，见 README「端口及如何修改」。

## 10. 卸载

```bash
systemctl --user disable --now ews-bridge.service ewscaldav.service ldapgal.service check-ews-url.timer
rm -f ~/bin/ews_bridge.py ~/bin/ewscaldav.py ~/bin/ldapgal.py ~/bin/check-ews-url.sh
rm -f ~/.config/ews-bridge/cred.json
# 如需彻底还原 TB：恢复 prefs.js 备份，或删除对应账户/日历/地址簿
```
