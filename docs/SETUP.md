# Setup & Usage Guide (from scratch)

> 中文：[SETUP.zh-CN.md](SETUP.zh-CN.md)

This guide is written for **new colleagues (and their AI agents)**. Following the
steps below on a **fresh Linux machine** should be enough to deploy and configure
the full "Thunderbird ↔ Exchange 2010" integration.

## Current architecture (important)

**Mail/Calendar use DavMail gateway; contacts use local LDAP bridge**:

| Function | Protocol / port | Notes |
|----------|-----------------|-------|
| Mail (receive) | IMAP `127.0.0.1:1143` | via DavMail → EWS |
| Mail (send) | SMTP `127.0.0.1:1025` | via DavMail → EWS |
| Calendar | CalDAV `127.0.0.1:1080` | via DavMail → EWS |
| Contacts (GAL) | LDAP `127.0.0.1:17089` | local bridge `ldapgal.py` → EWS |
| Archive | IMAP `127.0.0.1:17143` | via `davmail-filter` whitelist `/archive/` → DavMail |

> The legacy EWS mail bridge (`ews_bridge.py:17080`) and the self-hosted CalDAV
> bridge (`ewscaldav.py:17081`) are **disabled**; their code is kept for reference
> only. DavMail LDAP (`1389`) is also disabled — contacts use `ldapgal.py` instead.
> For migrating from the old EWS/IMAP-direct mode, see
> [MIGRATION-IMAP.md](MIGRATION-IMAP.md).

---

## 0. Prerequisites

- Linux desktop (Kylin / Ubuntu / others; ARM aarch64 and x86_64 both verified).
- **Python 3.8+** (stdlib only, no pip deps) and **`curl`**.
- An **Exchange 2010** account (mailbox + password) with:
  - IMAP/SMTP available (`mail.example.com:993` / `mail.example.com:587`);
  - browser sign-in at `https://mail.example.com/owa/`;
  - internal DNS resolving `mail.example.com`.

> **Key fact**: Exchange 2010 is a legacy server (IIS 7.5) that only supports
> **TLS 1.0**. This makes modern Thunderbird fail to connect — see §5 "TLS version".

---

## 1. Install Thunderbird

```bash
sudo snap install thunderbird        # or: sudo apt install thunderbird
```

**Version requirement**: native **CalDAV / LDAP** account types (any modern TB).

> No third-party add-ons needed. Mail uses native IMAP/SMTP; calendar/contacts use
> native CalDAV/LDAP.

---

## 2. Deploy the local bridges (contacts only)

Calendar is now served by **DavMail CalDAV** (external flatpak service), so only
the contacts LDAP bridge needs to be deployed locally.

### 2.1 Clone and deploy to a stable path

> **Important**: scripts must live in a path the OS won't clean up (e.g.
> `/opt/ews-bridge/`), **not** `~/bin` (which was wiped once by a system upgrade).

```bash
git clone https://github.com/<your-org>/tb-ews-bridge.git
cd tb-ews-bridge

sudo mkdir -p /opt/ews-bridge
sudo cp ldapgal.py ews_bridge.py /opt/ews-bridge/
sudo chmod +x /opt/ews-bridge/*.py
sudo chown -R $(whoami):$(whoami) /opt/ews-bridge
```

> `ldapgal.py` `import ews_bridge.py` (to reuse its EWS channel),
> so `ews_bridge.py` must be deployed too (even if not run as a service).
> `ewscaldav.py` is no longer deployed — Calendar uses DavMail CalDAV instead.

### 2.2 Configure credentials

```bash
mkdir -p ~/.config/ews-bridge
cp cred.json.example ~/.config/ews-bridge/cred.json
$EDITOR ~/.config/ews-bridge/cred.json    # fill in real values
chmod 600 ~/.config/ews-bridge/cred.json
```

`cred.json` fields:

| Field | Meaning |
|-------|---------|
| `host` / `port` / `sni` | Exchange hostname, 443, SNI (usually same host) |
| `user` / `password` | Domain account (UPN, e.g. `you@example.com`) and password |
| `listen` / `log` | Bind address `127.0.0.1` / log file |
| `ldap_port` | LDAP bridge port, default `17089` |
| `ldap_base` | GAL Base DN, e.g. `dc=example,dc=com` |

> `cred.json` is git-ignored — never commit it.

### 2.3 Configure systemd user services

```bash
mkdir -p ~/.config/systemd/user
cp systemd/ldapgal.service systemd/davmail-filter.service ~/.config/systemd/user/

# ensure ExecStart points at /opt/ews-bridge/ (the repo template already does)
systemctl --user daemon-reload
systemctl --user enable --now ldapgal.service davmail-filter.service
```

> Do **not** enable `ewscaldav.service` (Calendar now uses DavMail CalDAV `1080`).
> Do **not** enable `ews-bridge.service` (mail now uses DavMail IMAP/SMTP).
> Do **not** enable `check-ews-url.timer` (guarded the old EWS relay, no longer used).

### 2.4 Self-check

```bash
ss -ltn | grep -E '1143|1025|1080|17089|17143'   # all ports should listen

# LDAP probe
ss -ltn | grep -q '17089' && echo "LDAP bridge OK"

# CalDAV probe via DavMail (expect 207)
curl -s -o /dev/null -w '%{http_code}\n' -X PROPFIND -H 'Depth: 0' \
  --data '<D:propfind xmlns:D="DAV:"><D:prop><D:resourcetype/></D:prop></D:propfind>' \
  http://127.0.0.1:1080/dav/you@example.com/exchange/
```

---

## 3. Thunderbird: create the IMAP mail account

1. **Account Settings → New → Mail**, enter name, email, password, continue.
2. If autodiscovery fails, configure **manually**:
   - Account type: **IMAP** (receive)
   - Server: `127.0.0.1`, port `1143`, **No encryption** (local loopback)
   - Username: full email `you@example.com`
3. Outgoing server (SMTP):
   - Server: `127.0.0.1`, port `1025`, **No encryption**
   - Authentication: **Normal password** (not NTLM)
   - Username: full email `you@example.com`

> **SMTP auth must be "Normal password"**: Exchange supports `AUTH LOGIN`, not NTLM.
> See [TROUBLESHOOTING.md](TROUBLESHOOTING.md) §2.

### ⚠️ Important: Exchange 2010 SMTP 587 (STARTTLS) often fails

Exchange 2010's native SMTP over port **587 STARTTLS is frequently unreliable**—
connections often drop, time out, or are rejected by the server.

**Recommended solution:** route SMTP through the **DavMail gateway** (localhost:1025)
instead of connecting directly to `mail.example.com:587`. DavMail converts the
local SMTP connection into EWS calls to Exchange, which is far more stable.

Configure Thunderbird's **Outgoing server (SMTP)** as:

| Field | Value |
|-------|-------|
| Server | `127.0.0.1` |
| Port | `1025` |
| Connection security | **None** (local loopback, no TLS needed) |
| Authentication | Normal password |
| Username | `you@your-company.com` |

> **Why not use port 25?** Port 25 is often blocked by ISPs/firewalls and transmits
> mail in plaintext without encryption. Routing SMTP through DavMail keeps the
> submission local (`127.0.0.1:1025`) and lets DavMail handle the EWS submission
> to Exchange securely.

---

## 4. Fix folder mapping (Sent / Drafts)

Exchange uses **English** folder names (`Sent`, `Drafts`) while Thunderbird may
default to localized names (UTF-7-encoded internally). You must point them manually:

```
Account Settings → Copies & Folders
→ Sent copy → Other → pick server "Sent"
→ Drafts → Other → pick server "Drafts"
```

Equivalent `prefs.js` (edit with TB fully closed):
```javascript
user_pref("mail.identity.idN.fcc_folder",
          "imap://you%40example.com@mail.example.com/Sent");
user_pref("mail.identity.idN.draft_folder",
          "imap://you%40example.com@mail.example.com/Drafts");
```

> Without this, sent mail only exists locally and never appears in the web UI's
> Sent folder.

---

## 5. Critical network settings (TLS version + proxy)

**This is the most common pitfall in an Exchange 2010 environment.**

### 5.1 Allow TLS 1.0

Exchange 2010 (IIS 7.5) only supports **TLS 1.0**; modern Thunderbird disables it.

`about:config` (or `prefs.js` with TB closed):
```javascript
user_pref("security.tls.version.min", 1);
```

### 5.2 Disable proxy

If the system runs clash/mihomo etc., TB defaults to "Use system proxy" and routes
internal mail traffic through the proxy incorrectly.

UI: `Settings → General → Network & Disk Space → Connection → Settings... → No proxy`
or `prefs.js`:
```javascript
user_pref("network.proxy.type", 0);
```

> **Quit Thunderbird fully before editing `prefs.js`**, otherwise it's overwritten.

## 6. Add the CalDAV calendar

1. **Calendar → New Calendar → Network (CalDAV)**, URL:
   `http://127.0.0.1:1080/dav/you@example.com/exchange/`
2. After subscribing, **right-click the calendar → Properties → enable
   "Offline Support"** (otherwise it stays empty).

### 6.1 Fix the calendar's mail identity (important)

If the console shows `Calendar ... has a dangling E-Mail identity configured`, the
calendar's associated identity no longer exists (e.g. after switching from EWS to
IMAP). Point it to the current identity:

```javascript
user_pref("calendar.registry.<calendar-id>.imip.identity.key", "id2");
```

> Without this, **sending meeting invitations fails**. See
> [TROUBLESHOOTING.md](TROUBLESHOOTING.md) §4.

---

## 7. Add the LDAP address book + compose autocomplete

1. **Address Book → New → LDAP Directory**:
   - Host `127.0.0.1`, Port `17089`, Base DN `dc=example,dc=com`
   - enable "search in address book when composing"
2. **Compose autocomplete**: `Settings → Composition → Addressing`, set
   **Directory Server** from `None` to that LDAP directory (equiv. `about:config`:
   `ldap_2.autoComplete.useDirectory=true` +
   `ldap_2.autoComplete.directoryServer=ldap_2.servers.<name>`).

---

## 8. Verification checklist

- [ ] `ss -ltn` shows `1143` / `1025` / `1080` / `17089` / `17143` listening
- [ ] TB **sends/receives mail** (IMAP receive via DavMail `1143`, SMTP send via DavMail `1025`)
- [ ] **Sent mail** appears in the web UI (OWA)
- [ ] **Calendar** can create/edit events (not read-only), offline support enabled
- [ ] **Address book** finds people, compose autocomplete works
- [ ] Console has no `SSL_ERROR_UNSUPPORTED_VERSION`, no `dangling E-Mail identity`

---

## 9. Quick troubleshooting

| Problem | Quick fix | See |
|---------|-----------|-----|
| Connection error / SSL_ERROR_UNSUPPORTED_VERSION | enable TLS 1.0 | [TROUBLESHOOTING](TROUBLESHOOTING.md) §1 |
| SMTP auth failure | use "Normal password" | [TROUBLESHOOTING](TROUBLESHOOTING.md) §2 |
| Sent folder mismatch | map fcc_folder to `Sent` | [TROUBLESHOOTING](TROUBLESHOOTING.md) §3 |
| Calendar dangling identity | fix `imip.identity.key` | [TROUBLESHOOTING](TROUBLESHOOTING.md) §4 |
| Calendar read-only | PROPFIND write privileges | [TROUBLESHOOTING](TROUBLESHOOTING.md) §5 |
| Calendar empty | enable Offline Support | [TROUBLESHOOTING](TROUBLESHOOTING.md) §6 |

Full guide: [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

---

## 10. Uninstall

```bash
systemctl --user disable --now ldapgal.service davmail-filter.service
sudo rm -rf /opt/ews-bridge
rm -f ~/.config/ews-bridge/cred.json
# Thunderbird: remove the account / calendar / address book
```

---

*Doc version: 2.0.0 (IMAP mode) | Updated: 2026-10-16 | Maintainer: gic-commits*
