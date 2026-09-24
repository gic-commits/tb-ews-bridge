# Contributing

感谢参与！本项目是 Thunderbird ↔ Exchange 2010 的本地协议桥，欢迎 Issue 与 PR。

## 开发环境

- Python 3.8+（仅标准库，无第三方依赖）
- `curl`（用于 EWS 的 NTLM 握手）
- 一个可访问的 Exchange 2010 账号（EWS 端点）用于联调

## 本地运行

```bash
# 1) 配置凭据（切勿提交）
mkdir -p ~/.config/ews-bridge
cp cred.json.example ~/.config/ews-bridge/cred.json
$EDITOR ~/.config/ews-bridge/cred.json      # 填 host/user/password 等
chmod 600 ~/.config/ews-bridge/cred.json

# 2) 启动
python3 ews_bridge.py     # :17080
python3 ewscaldav.py      # :17081
python3 ldapgal.py 17089   # :17089
```

自测脚本：

```bash
python3 tests/ews_contacts_test.py   # 需先配置 cred.json
python3 tests/ldap_smoke_test.py     # 需 ldapgal.py 已在 17089 运行
python3 -m py_compile ews_bridge.py ewscaldav.py ldapgal.py
```

## 约定

- **不要提交任何真实凭据、内网主机名、公司/个人信息**；仓库内一律用
  `example.com` / `you@your-company.com` 等占位符。
- 保持**标准库优先**；如确需新增依赖，请在 PR 说明理由。
- 涉及 EWS 行为（元素顺序、FieldURI 命名空间、ChangeKey、权限宣告等）的改动，
  请附上最小复现与验证方式。
- 日志中避免打印密码、邮箱正文等敏感内容。

## 提交 PR

1. Fork 并从 `main` 建分支；
2. 保持改动聚焦，说明"现象 / 根因 / 修复 / 验证"；
3. 确保 `py_compile` 通过、无敏感信息；
4. 提交信息简洁，描述做了什么与为什么。
