# 挂起/唤醒卡顿修复文档

## 概述

2026-10-14 设备 S3 深度休眠唤醒后，UKUI 桌面卡死约 6 分钟（锁屏无响应、鼠标键盘失灵），SSH 正常。根因定性为：**UKUI 锁屏/会话组件唤醒 Bug + NAS FUSE 挂载并发 + 多服务并发重连抖动** 叠加。

已落地四项确定性修复，验证有效。

---

## 1. EWS 看门狗路径修正 + TB_PROFILE 补全

**文件**：`~/.config/systemd/user/check-ews-url.service`

**变更**：
- `ExecStart: %h/bin/check-ews-url.sh → /opt/ews-bridge/check-ews-url.sh`
- 新增 `Environment=TB_PROFILE=g93eb7lq.default`

**验证**：
```bash
systemctl --user start check-ews-url.service
# → status=0/SUCCESS，prefs.js 三处 ews_url 均指向 http://127.0.0.1:17080/ews/exchange.asmx
```

---

## 2. 新增用户桥接服务挂起/唤醒钩子

**文件**：`/usr/lib/systemd/system-sleep/60-user-bridges` (755)

**逻辑**：
- `pre/*`：`systemctl --user stop ews-bridge ewscaldav ldapgal`
- `post/*`：等待网络可达（最长 10s，双探测：`8.8.8.8:53` + `mail.example.com:443`）→ `systemctl --user start` 同三服务

**日志**：`/var/log/user-bridges-sleep.log`（含时间戳）

**验证**：
```bash
sudo /usr/lib/systemd/system-sleep/60-user-bridges pre suspend   # 服务停止
sudo /usr/lib/systemd/system-sleep/60-user-bridges post suspend  # 等网络后启动
cat /var/log/user-bridges-sleep.log
# → 含时间戳的 suspend/resume 记录
```

**目的**：消除唤醒瞬间三桥服务并发 30s 超时重连抖动，释放唤醒窗口 CPU/网络。

---

## 3. NAS 挂载探测加速 + 去重唤醒触发

**文件**：
- `/etc/systemd/system/nas-webdav-rclone.service`（探测循环 6×3s → 3×2s，上限 36s→12s）
- `/usr/lib/systemd/system-sleep/50-nas-webdav`（移除 post-resume reconcile，仅 restart watchdog；保留 pre 卸载）

**目的**：消除唤醒时三路并发触发挂载（NM dispatcher ×3 + sleep hook reconcile + watchdog），避免 rclone ExecStartPre 并发跑满 36s 并 Hang 住桌面访问。

---

## 4. 关闭 TearFree（软渲桌面唤醒重绘开销极大）

**文件**：`/etc/X11/xorg.conf.d/20-ftd330-nofree.conf`

```conf
Section "OutputClass"
    Identifier "FTD330"
    MatchDriver "ftd330"
    Option "TearFree" "false"
EndSection
```

**原因**：FT3300/D3000 仅有 modesetting 2D DDX (`ftd330_drv.so`) + VAAPI 视频解码，**无 3D 驱动 (`ftd330_dri.so` 缺失)**，Xorg 回退 DRISWRAST 软渲。TearFree 在软渲下需额外内存拷贝/合成，唤醒后全屏重绘极慢。关闭后唤醒首帧绘制显著加快。

---

## 文件清单

| 文件 | 类型 | 状态 |
|------|------|------|
| `~/.config/systemd/user/check-ews-url.service` | 用户服务 | ✅ 修改 |
| `/usr/lib/systemd/system-sleep/60-user-bridges` | 系统睡眠钩子 | ✅ 新建 |
| `/etc/systemd/system/nas-webdav-rclone.service` | 系统服务 | ✅ 修改 |
| `/usr/lib/systemd/system-sleep/50-nas-webdav` | 系统睡眠钩子 | ✅ 修改 |
| `/etc/X11/xorg.conf.d/20-ftd330-nofree.conf` | Xorg 配置片段 | ✅ 新建 |

---

## 验收命令

```bash
# 1) EWS 看门狗
systemctl --user start check-ews-url.service
# → Active: inactive (dead) ... status=0/SUCCESS

# 2) rclone 探测上限 12s
sudo systemctl restart nas-webdav-rclone.service
# → ExecStartPre ... for i in 1 2 3; timeout 2 ... status=0/SUCCESS

# 3) 睡眠钩子就位
ls -l /usr/lib/systemd/system-sleep/60-user-bridges /usr/lib/systemd/system-sleep/50-nas-webdav
# → 均 755

# 4) TearFree 已关
cat /etc/X11/xorg.conf.d/20-ftd330-nofree.conf
# → Option "TearFree" "false"

# 5) sleep hook 日志
cat /var/log/user-bridges-sleep.log
```

---

## 仓库同步项

| 仓库文件 | 同步内容 |
|----------|----------|
| `systemd/ews-bridge.service` | ExecStart → `/opt/ews-bridge/ews-bridge.py` |
| `systemd/ewscaldav.service` | ExecStart → `/opt/ews-bridge/ewscaldav.py`；`EWS_BRIDGE_PATH=/opt/ews-bridge/ews-bridge.py` |
| `systemd/ldapgal.service` | ExecStart → `/opt/ews-bridge/ldapgal.py`；`EWS_BRIDGE_PATH=/opt/ews-bridge/ews-bridge.py` |
| `systemd/check-ews-url.service` | ExecStart → `/opt/ews-bridge/check-ews-url.sh` |

---

## 回滚指南

```bash
# 1) 恢复 EWS 看门狗原路径
sed -i 's|ExecStart=/opt/ews-bridge/check-ews-url.sh|ExecStart=%h/bin/check-ews-url.sh|' \
  ~/.config/systemd/user/check-ews-url.service
sed -i '/Environment=TB_PROFILE/d' ~/.config/systemd/user/check-ews-url.service
systemctl --user daemon-reload

# 2) 删除睡眠钩子
sudo rm -f /usr/lib/systemd/system-sleep/60-user-bridges

# 3) 恢复 rclone 探测循环
sudo sed -i "s|for i in 1 2 3; do timeout 2|for i in \$(seq 1 6); do timeout 3|" \
  /etc/systemd/system/nas-webdav-rclone.service
sudo sed -i "s|sleep 2; done|sleep 3; done|" \
  /etc/systemd/system/nas-webdav-rclone.service
sudo systemctl daemon-reload

# 4) 恢复 50-nas-webdav post reconcile
sudo sed -i '/post)/,/;;/s|# 不再在此 reconcile.*||; /post)/a\    systemctl restart nas-webdav-watch.service 2>/dev/null\n    /usr/local/bin/nas-webdav-reconcile.sh >/dev/null 2>&1 &' \
  /usr/lib/systemd/system-sleep/50-nas-webdav

# 5) 恢复 TearFree
sudo rm -f /etc/X11/xorg.conf.d/20-ftd330-nofree.conf
```

---

## 后续建议（给维护方）

1. **UKUI 锁屏唤醒**：建议上游修复 `ukui-screensaver-dialog` 与 `ukcc.session` 在 S3 resume 路径的死锁/超时。
2. **NAS FUSE 防 Hang**：考虑给 rclone mount 加 `--network-mode` 或 systemd `TimeoutStartSec` 实现挂载超时自动回滚。
3. **kytensor 唤醒策略**：仅在显式调用时按需加载模型，而非每次唤醒全量重载 ASR 模型。
4. **FT3300 3D 驱动**：确认上游是否规划发布 `ftd330_dri.so` (Mesa Gallium)；现状仅 2D + VAAPI。

---

*记录时间：2026-10-14*
*适用环境：Kylin V10 SP1 / Phytium FT3300 (D3000M) / UKUI / systemd 252+*
