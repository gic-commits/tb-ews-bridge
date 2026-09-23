# Thunderbird ↔ Exchange 2010 Bridge

Use an enterprise **Exchange 2010** service from **Thunderbird** (including the
snap build) with **no third-party add-ons**:

- **Mail** — via a local EWS relay that works around the legacy server;
- **Calendar** — via a self-hosted **CalDAV server** bridge (read + write,
  meeting invitations);
- **Address book (GAL)** — via a self-hosted read-only **LDAP server** bridge
  for compose-time autocomplete.

> 中文说明：[README.zh-CN.md](README.zh-CN.md) ｜ Design: [docs/DESIGN.md](docs/DESIGN.md) ｜
> Changes: [CHANGELOG.md](CHANGELOG.md)

---

## 1. Background

**Modern Thunderbird natively supports Microsoft Exchange (EWS) mail**:
account type `ews` with Autodiscover (`email-exchange-type.mjs`,
`ExchangeAutoDiscover.sys.mjs`, `IExchangeOutgoingServer`, `ews_url`). That
implementation, however, targets **newer** Exchange servers.

This deployment runs **Exchange 2010 SP3 (end-of-life)**. Its EWS differs from
modern Exchange in authentication, schema, and write validation, so connecting
directly with native EWS does not work reliably (e.g. writes are rejected by a
read-only validation with `ErrorChangeKeyRequiredForWriteOperations`). In
addition, current Thunderbird's native EWS covers **mail only** — there is no
EWS calendar/contacts provider (calendar has only CalDav/ICS, etc.).

This project therefore adds a **local compatibility / translation bridge**:

- **Mail**: Thunderbird's native EWS points at the local bridge; the bridge
  performs the **NTLM handshake** and **rewrites requests** to accommodate the
  legacy server (a compatibility shim, not a protocol translation).
- **Calendar / Contacts**: switched to Thunderbird-native **CalDAV / LDAP**,
  translated to EWS by the bridge.

```
Thunderbird ──(native EWS    :8080)──> ews_bridge.py ──(NTLM)──> mail.example.com:443 EWS
Thunderbird ──(native CalDAV :8081)──> ewscaldav.py   ──┘
Thunderbird ──(native LDAP   :1389)──> ldapgal.py     ──┘
```

Core principle: **zero add-ons on the TB side, native protocols only; all
EWS-specific knowledge and compatibility handling live in the local bridge.**

## 2. Components

| File | Port | Description |
|---|---|---|
| `ews_bridge.py` | 8080 | EWS relay: NTLM handshake (delegated to `curl --ntlm`), request-body rewrite, built-in DNS, per-operation EWS logging |
| `ewscaldav.py` | 8081 | CalDAV server: PROPFIND / REPORT (multiget, calendar-query) / GET / PUT / DELETE; EWS `FindItem`+`CalendarView` → ICS; UID↔ItemId/ChangeKey in SQLite; write-back via `CreateItem`/`UpdateItem`/`DeleteItem` |
| `ldapgal.py` | 1389 | Read-only LDAP v3 (Bind/Search, minimal BER codec): filter extraction → EWS `ResolveNames` (GAL), 90s cache, ≤50 entries per query |
| `check-ews-url.sh` | - | EWS bridge URL guard: verifies/restores TB's `ews_url` to the local bridge (with a 10-min systemd timer) |
| `tests/` | - | Self-tests: EWS contacts/ResolveNames probing, LDAP Bind+Search smoke test |

## 3. Status

### 3.1 Mail
Thunderbird connects as an Exchange (EWS) account through the 8080 bridge;
`POST …/exchange.asmx` uses `curl --ntlm`. Request rewriting works around EWS
2010's read-only validation (`ErrorChangeKeyRequiredForWriteOperations`).

### 3.2 Calendar (read + write)
- **Read**: hierarchy discovery (principal → calendar-home → calendar),
  `calendar-multiget`, `calendar-query(time-range)`, single/full GET. Window
  ±180 days, ≤1200 events.
- **Write-back**: TB create/edit/delete → `PUT`/`DELETE` → EWS
  `CreateItem`/`UpdateItem`/`DeleteItem`.
- **Meeting invitations**: when the ICS has `ATTENDEE`s — create
  `SendToAllAndSaveCopy`, update `SendToChangedAndSaveCopy`, delete
  `SendToAllAndSaveCopy`; no attendees → `SendToNone`.
- **Timezones**: TB sends local wall-clock time (`TZID=Asia/Shanghai` or
  floating) → the bridge converts to UTC (`Z`) at +08:00; UTC/all-day kept as-is.
- **Attachments**: URI attachments (`ATTACH:http://…`) are persisted as a body
  marker and restored to `ATTACH` on read; file attachments
  (`ATTACH;ENCODING=BASE64`) are implemented but see §5.

### 3.3 Address book (read-only GAL)
Exchange `ResolveNames` is a *search* (not an enumeration), which aligns with
LDAP `SearchRequest`, so LDAP is used instead of CardDAV. The filter extractor
takes the longest search term → `ResolveNames`.

## 4. Quick Start

### 4.1 Credentials (never in source)

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

> `cred.json` is git-ignored — never commit it. See `cred.json.example`.

### 4.2 Run the services

```bash
python3 ews_bridge.py                 # EWS relay (8080)
python3 ewscaldav.py                  # CalDAV calendar (8081)
python3 ldapgal.py 1389               # LDAP address book (1389)
```

systemd user-unit templates are in `systemd/`.

### 4.3 Thunderbird setup

1. **Mail**: create an Exchange (EWS) account and point `ews_url` at
   `http://127.0.0.1:8080/ews/exchange.asmx` (this value is not in the UI; use
   `check-ews-url.sh` to keep it guarded).
2. **Calendar** → New Calendar → Network Calendar (CalDAV), URL:
   `http://127.0.0.1:8081/dav/you@your-company.com/exchange/`
3. **Address book** → New → LDAP Directory: Host `127.0.0.1`, Port `1389`,
   Base DN `dc=example,dc=com` (enable "search in address book when composing").
4. **Compose autocomplete** → Settings → Composition → Addressing: change
   **Directory Server** from `None` to that LDAP directory (equivalently, in
   about:config set `ldap_2.autoComplete.useDirectory=true` and
   `ldap_2.autoComplete.directoryServer=…`).

### 4.4 Guarding the EWS bridge URL (optional)

```bash
TB_PROFILE=~/.thunderbird/<profile> ./check-ews-url.sh      # check/restore
TB_PROFILE=~/.thunderbird/<profile> ./check-ews-url.sh -n   # check only (dry run)
systemctl --user enable --now check-ews-url.timer           # run every 10 min
```

## 5. Known Limitations

- **Calendar attachments: TB's front-end is incomplete.** The event dialog's
  attachment menu only offers "URL" (`chrome/calendar/content/
  calendar-event-dialog.xhtml` has only `cmd_attach_url`, plus a disabled
  `cmd_attach_cloud` placeholder). Local-file attachments therefore cannot be
  added in TB; the bridge's file-attachment (base64) code is kept but not
  triggered by TB. It will work once upstream completes the UI.
- **Duplicate events from accepted invitations**: Exchange 2010 calendar items
  carry no iCalendar UID, so the bridge generates a UUID; when the user
  *accepts* an invitation from mail, TB creates a local event with the
  invitation's original UID → possible duplicates. Fixing this needs "original
  UID alignment" (see Roadmap).
- **Server is Exchange 2010**: EWS SOAP only, relies on `curl --ntlm`, no
  modern authentication.
- **Single account, loopback only**: all three services bind `127.0.0.1`.
- **etag versioning**: when the read-back serialization changes (e.g. adding
  `ATTENDEE`), the `etag()` suffix must be bumped to force one client refetch,
  otherwise clients keep their cache under the old etag.

## 6. Key Design Notes (see docs/DESIGN.md)

- **CalDAV interop traps**: TB sends uppercase `<D:href>` (regex must use
  `re.I`); REPORT is often **chunked** (chunked decoding is required, otherwise
  the body is always empty).
- **Read/write privileges**: `PROPFIND` must advertise
  `current-user-privilege-set` with `write/write-content/bind/unbind`, or TB
  forces the calendar read-only on every start (and that flag is "set-only").
- **Write-back pitfalls**: `CalendarItem` children must follow schema order;
  `UpdateItem` FieldURIs are namespaced (`item:*` vs `calendar:*`); writes
  require a `ChangeKey`; an empty `OptionalAttendees` triggers
  `ErrorSchemaValidation`.
- **`FindItem` returns no Body/structured attendees**: bodies are filled via a
  batch `GetItem(IdOnly+item:Body)`, attendees come from the local mapping
  store — avoiding per-event `GetItem` that would slow down sync.
- **Credential safety**: the password lives only in
  `~/.config/ews-bridge/cred.json` (mode 600).

## 7. Roadmap

- [x] M0 Mail bridge (NTLM relay + request rewrite + operation logging)
- [x] M1 Read-only calendar (CalDAV discovery/query/multiget)
- [x] M2 Calendar write-back (PUT/DELETE → EWS Create/Update/Delete)
- [x] M3 Meeting invitations (attendees → invite/update/cancel)
- [x] M4 Read-only GAL (LDAP → ResolveNames)
- [x] M5 Timezone fix, URI attachments, calendar read/write privileges
- [ ] M-UID alignment: remove calendar duplicates from accepted invitations
- [ ] (upstream) local-file attachments in the TB event dialog

## 8. Tests

```bash
python3 tests/ews_contacts_test.py   # probe ResolveNames fields and server constraints
python3 tests/ldap_smoke_test.py     # craft LDAP Bind+Search; verify filter extraction and entries
```

## 9. Security & Disclaimer

- For use with your own account on an internal/loopback network only;
  `cred.json` is git-ignored — do not commit it.
- All services bind `127.0.0.1` and are not exposed externally.
- Not affiliated with Microsoft or Mozilla; for interoperability research only.

## 10. License

[MIT](LICENSE). See [CONTRIBUTING.md](CONTRIBUTING.md) for contributions.
