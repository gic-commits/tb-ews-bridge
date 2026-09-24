#!/bin/bash
#
# check-ews-url.sh — 检查并恢复 Thunderbird 的 EWS 桥地址
#
# 背景：TB/owl 通过 prefs.js 里的 ews_url 指向本地 EWS 桥
# (http://127.0.0.1:17080/ews/exchange.asmx)。该值无法在 UI 配置，
# 若被（如宿主名/URL 修改、扩展重置）改回真实域名 mail.example.com，
# TB 会绕过本桥直连内网/公网服务器，导致邮件失效。
# 本脚本检测异常并自动恢复到桥地址，同时留下备份。
#
# 用法:
#   ./check-ews-url.sh          # 检查；有异常则恢复并输出日志
#   ./check-ews-url.sh -n      # 仅检查，不恢复（dry run）
#
# 配合 systemd timer 定时巡检: systemd/check-ews-url.timer + .service

set -u

PREF="$HOME/.thunderbird/${TB_PROFILE:?set TB_PROFILE to your profile dir}/prefs.js"
LOG="$HOME/.cache/check-ews-url.log"
BRIDGE_URL="http://127.0.0.1:17080/ews/exchange.asmx"
EXPECTED_PREFS=(
  'mail.server.server2.ews_url'
  'mail.outgoingserver.ews1.ews_url'
)

DRY=0
[ "${1:-}" = "-n" ] && DRY=1

mkdir -p "$(dirname "$LOG")"
touch "$LOG"

restore() {
  local pref="$1"
  local now
  now=$(date '+%F %T')
  cp -a "$PREF" "${PREF}.bak-checkews"
  echo "[$now] restore: $pref -> $BRIDGE_URL" >> "$LOG"
  echo "[$now]  restore $pref -> $BRIDGE_URL"
}

check_pref() {
  local pref="$1"
  local val
  val=$(grep -oP "\"$pref\",\s*\"\K[^\"]*" "$PREF" 2>/dev/null)

  if [ -z "$val" ]; then
    echo "[$(date '+%F %T')] absent: $pref (not set) — 未做改动" >> "$LOG"
    echo "  缺少 $pref (未设置)，跳过"
    return 0
  fi

  if [ "$val" != "$BRIDGE_URL" ]; then
    echo "[$(date '+%F %T')] mismatch: $pref = \"$val\"" >> "$LOG"
    echo "  异常: $pref = \"$val\""
    if [ "$DRY" = 1 ]; then
      echo "  (dry run) 应改为: $BRIDGE_URL"
      return 1
    fi
    sed -i "s#\(\"$pref\", \"\)[^\"]*#\1$BRIDGE_URL#" "$PREF"
    restore "$pref"
    return 1
  fi
  echo "  OK: $pref = \"$val\""
}

echo "== check-ews-url: prefs = $PREF (dry=$DRY)"
echo "== 期望 EWS 桥地址: $BRIDGE_URL"
bad=0
for p in "${EXPECTED_PREFS[@]}"; do
  if ! check_pref "$p"; then bad=1; fi
done

# mail.ews.server_versions 缓存: 期望含桥地址键。仅提示，不自动改，
# 避免破坏 TB 的 Exchange 版本映射表。
if grep -q '127.0.0.1:17080/ews/exchange.asmx' "$PREF"; then
  echo "  OK: server_versions 含桥地址"
else
  echo "  notice: server_versions 未含桥地址"
fi

[ "$bad" = 1 ] && [ "$DRY" = 0 ] && echo ">> 已恢复，请重启 Thunderbird 使新地址生效。"
exit 0