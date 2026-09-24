# Setup & Usage Guide (from scratch)

> 中文：[SETUP.zh-CN.md](SETUP.zh-CN.md)

This walks through, in order: installing Thunderbird → deploying the local
bridge → creating an Exchange (EWS) account → pointing it at the bridge → adding
calendar / address book → running as services with a guard.

## 0. Prerequisites

- A Linux desktop (verified on Kylin/ARM aarch64; x86_64 works too).
- **Python 3.8+** (standard library only, no pip dependencies) and **`curl`**.
- An **Exchange 2010** account (mailbox + password) with EWS available:
  - `https://<exchange-host>/EWS/Exchange.asmx` is reachable (returns 401 when
    unauthenticated);
  - you can sign in to `https://<exchange-host>/owa/` in a browser.
- Internal DNS resolution for `<exchange-host>`.

## 1. Install Thunderbird

- snap: `sudo snap install thunderbird`
- or distro package: `sudo apt install thunderbird`
- or download from the official site.

**Version requirement**: native Exchange (EWS) **mail** support. Check by whether
an "Exchange" account type is offered, or whether `about:config` has `ews` keys.
Older Thunderbird builds lack it.

> Note: native EWS covers **mail only**; there is no EWS calendar/contacts
> provider, which is why this project bridges them via CalDAV / LDAP.

## 2. Deploy the local bridge

```bash
git clone https://github.com/<you>/tb-ews-bridge.git
cd tb-ews-bridge

mkdir -p ~/bin ~/.config/ews-bridge
cp ews_bridge.py ewscaldav.py ldapgal.py check-ews-url.sh ~/bin/
chmod +x ~/bin/check-ews-url.sh

cp cred.json.example ~/.config/ews-bridge/cred.json
$EDITOR ~/.config/ews-bridge/cred.json      # fill in real values (table below)
chmod 600 ~/.config/ews-bridge/cred.json
```

`cred.json` fields:

| Field | Meaning |
|---|---|
| `host` / `port` / `sni` | Exchange hostname, 443, SNI (usually the same host) |
| `user` / `password` | Domain account (UPN, e.g. `you@your-company.com`) and password |
| `lport` | Local mail EWS bridge port, default `17080` |
| `ldap_port` | LDAP bridge port, default `17089` |
| `ldap_base` | GAL Base DN, e.g. `dc=example,dc=com` |
| `listen` / `log` | Bind address (`127.0.0.1`) / log file |

## 3. Start and self-check

```bash
python3 ~/bin/ews_bridge.py      # mail EWS relay  17080
python3 ~/bin/ewscaldav.py       # CalDAV calendar 17081
python3 ~/bin/ldapgal.py         # LDAP address book 17089
```

In another terminal:

```bash
ss -ltn | grep -E '17080|17081|17089'          # three ports listening
curl -s -o /dev/null -w '%{http_code}\n' -X PROPFIND \
  -H 'Depth: 0' --data '<D:propfind xmlns:D="DAV:"><D:prop><D:resourcetype/></D:prop></D:propfind>' \
  http://127.0.0.1:17081/dav/you@your-company.com/exchange/   # expect 207
```

## 4. Thunderbird: create the Exchange (EWS) mail account

1. **Account Settings → New → Mail**, enter name, email address, password,
   continue.
2. Thunderbird will try **Autodiscover**. Against a legacy Exchange 2010 this may
   fail or point at the real domain; if so, configure **manually**:
   - account type **Exchange (EWS)**;
   - server = the real `host`, username = UPN, port 443 + SSL.
3. **Key step — point `ews_url` at the local bridge** (not exposed in the UI, so
   edit the config):
   - **Fully quit Thunderbird** (confirm `pgrep thunderbird` prints nothing);
   - edit `~/.thunderbird/<profile>/prefs.js` (e.g. `xxxx.default-release`):
     - `mail.server.serverN.ews_url` → `http://127.0.0.1:17080/ews/exchange.asmx`
     - `mail.outgoingserver.ewsN.ews_url` → the same
   - Reopen Thunderbird; if you can send/receive, it worked.
4. Use the `check-ews-url.timer` from `<repo>/systemd/` to guard this value
   against being reverted to the real domain (see §7).

> The local bridge performs the **NTLM handshake + request rewriting** to suit
> EWS 2010; on the Thunderbird side it is still its own native EWS.

## 5. Add the CalDAV calendar

1. **Calendar → New Calendar → Network (CalDAV)**, URL:
   `http://127.0.0.1:17081/dav/you@your-company.com/exchange/`
2. Subscribe. Then **right-click the calendar → Properties → enable
   "Offline Support"** — without it Thunderbird does not fetch/cache remote
   events and the calendar stays empty. You can now browse/edit events.
   If it shows as read-only, see §9.

## 6. Add the LDAP address book + compose autocomplete

1. **Address Book → New → LDAP Directory**:
   - Host `127.0.0.1`, Port `17089`, Base DN `dc=example,dc=com`;
   - enable "search in address book when composing".
2. **Compose autocomplete**: Settings → Composition → Addressing, change
   **Directory Server** from `None` to that LDAP directory (equivalently, in
   about:config set `ldap_2.autoComplete.useDirectory=true` and
   `ldap_2.autoComplete.directoryServer=ldap_2.servers.<name>`).

## 7. Run as services (systemd --user, autostart)

```bash
mkdir -p ~/.config/systemd/user
cp systemd/*.service systemd/*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ews-bridge.service ewscaldav.service ldapgal.service
systemctl --user enable --now check-ews-url.timer     # guard ews_url every 10 min
```

> Templates use `%h/bin/...`, matching `~/bin` from §2. If you changed ports, add
> the port argument to the `ExecStart` of `ewscaldav.service` / `ldapgal.service`
> (for the mail port, set `cred.json` → `lport`).

## 8. Verification checklist

- `ss -ltn` shows the three ports listening;
- Thunderbird **sends/receives mail**;
- the **calendar** can create/edit events (not read-only);
- the **address book** finds people, and compose autocomplete works.

## 9. Troubleshooting

- **Mail 401 / auth failure**: usually a locked domain account or an expired
  password. Verify via `https://<exchange-host>/owa/` in a browser; a lockout
  typically clears in ~30 minutes.
- **Calendar always read-only**: ensure the bridge's `PROPFIND` advertises
  `current-user-privilege-set` with `write` (this repo does); Thunderbird only
  re-evaluates privileges while `readOnly=false`.
- **Calendar shows no events**: right-click the calendar → Properties →
  enable **"Offline Support"**. Thunderbird does not populate a CalDAV
  calendar while it is off.
- **Meeting attachments only accept URLs**: a Thunderbird event-dialog limitation
  (no local-file entry point); the bridge's file-attachment (base64) code exists
  but is never triggered.
- **Duplicate events after accepting an invitation**: Exchange 2010 exposes no
  iCalendar UID via standard fields; see the M-UID note in the README's
  "Known Limitations".
- **Port conflicts**: see the README's "Ports and how to change them".

## 10. Uninstall

```bash
systemctl --user disable --now ews-bridge.service ewscaldav.service ldapgal.service check-ews-url.timer
rm -f ~/bin/ews_bridge.py ~/bin/ewscaldav.py ~/bin/ldapgal.py ~/bin/check-ews-url.sh
rm -f ~/.config/ews-bridge/cred.json
# To fully revert Thunderbird, restore your prefs.js backup or remove the
# account / calendar / address book.
```
