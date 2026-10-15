**English** | [简体中文](README.zh-CN.md)

# Thunderbird ↔ Exchange 2010 Bridge

Use an enterprise **Exchange 2010** service from **Thunderbird** (including the
snap build) with **no third-party add-ons**:

- **Mail** — via native **IMAP/SMTP** (993/587), replacing the EWS relay;
- **Calendar** — via a self-hosted **CalDAV server** bridge (read + write,
  meeting invitations);
- **Address book (GAL)** — via a self-hosted read-only **LDAP server** bridge
  for compose-time autocomplete.

> Design: [docs/DESIGN.md](docs/DESIGN.md) ｜ Changes: [CHANGELOG.md](CHANGELOG.md) ｜ IMAP Migration: [docs/MIGRATION-IMAP.md](docs/MIGRATION-IMAP.md)

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

- **Mail**: Thunderbird uses native **IMAP/SMTP** (993/587) — no bridge needed.
  The old EWS relay (`ews_bridge.py:17080`) is disabled; code retained for reference.
- **Calendar / Contacts**: Thunderbird native **CalDAV / LDAP**, translated to EWS by the bridge.

```
Thunderbird ──(IMAP/SMTP :993/587)──> mail.example.com:993/587
Thunderbird ──(原生 CalDAV:17081)──> ewscaldav.py   ──┘
Thunderbird ──(原生 LDAP  :17089)──> ldapgal.py     ──┘
```

Core principle: **zero add-ons on the TB side, native protocols only; all
EWS-specific knowledge and compatibility handling live in the local bridge.**

## 2. Components

| File | Port | Description |
|---|---|---|
| `ewscaldav.py` | 17081 | CalDAV server: PROPFIND / REPORT (multiget, calendar-query) / GET / PUT / DELETE; EWS `FindItem`+`CalendarView` → ICS; UID↔ItemId/ChangeKey in SQLite; write-back via `CreateItem`/`UpdateItem`/`DeleteItem` |
| `ldapgal.py` | 17089 | Read-only LDAP v3 (Bind/Search, minimal BER codec): filter extraction → EWS `ResolveNames` (GAL), 90s cache, ≤50 entries per query |
| `check-ews-url.sh` | - | EWS bridge URL guard: verifies/restores TB's `ews_url` to the local bridge (with a 10-min systemd timer) |
| `tests/` | - | Self-tests: EWS contacts/ResolveNames probing, LDAP Bind+Search smoke test |

> **Note**: `ews_bridge.py` (17080) is **disabled**; code retained for reference. Mail now uses native IMAP/SMTP. See [IMAP Migration Guide](docs/MIGRATION-IMAP.md).

## 3. Status

### 3.1 Mail (IMAP/SMTP)
Thunderbird connects via native **IMAP (993, SSL/TLS)** and **SMTP (587, STARTTLS)**.
The old EWS relay (`ews_bridge.py:17080`) is **disabled**; code retained for reference.
IMAP provides native IDLE push + 3-minute polling; SMTP uses port 587 with
STARTTLS and `AUTH LOGIN` (plain password) — no NTLM required.

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

> For a detailed, from-scratch walkthrough (install Thunderbird, create the IMAP
> account, point CalDAV/LDAP at the bridge, run as services), see
> [docs/SETUP.md](docs/SETUP.md).

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
  "lport": 17080,
  "log": "/tmp/ews-bridge.log",
  "ldap_port": 17089,
  "ldap_base": "dc=example,dc=com"
}
EOF
chmod 600 ~/.config/ews-bridge/cred.json
```

> `cred.json` is git-ignored — never commit it. See `cred.json.example`.

### 4.2 Run the services

```bash
python3 ewscaldav.py                  # CalDAV calendar (17081)
python3 ldapgal.py 17089               # LDAP address book (17089)
# ews_bridge.py is DISABLED — mail uses native IMAP/SMTP
```

systemd user-unit templates are in `systemd/`.

### 4.3 Thunderbird setup

1. **Mail**: create an **IMAP** account
   - Server: `mail.example.com`, Port: `993`, SSL/TLS, Normal password
   - SMTP: `mail.example.com:587`, STARTTLS, Normal password
   - Detailed config: see [IMAP Migration Guide](docs/MIGRATION-IMAP.md)

2. **Calendar** → New Calendar → Network Calendar (CalDAV), URL:
   `http://127.0.0.1:17081/dav/you@your-company.com/exchange/`

3. **Address book** → New → LDAP Directory: Host `127.0.0.1`, Port `17089`,
   Base DN `dc=example,dc=com` (enable "search in address book when composing").

5. **Compose autocomplete** → Settings → Composition → Addressing: change
   **Directory Server** from `None` to that LDAP directory (equivalently, in
   about:config set `ldap_2.autoComplete.useDirectory=true` and
   `ldap_2.autoComplete.directoryServer=…`).

### 4.2 Ports and how to change them

Defaults (all bind `127.0.0.1`, non-privileged):

| Service | Default port | Where it is configured |
|---|---|---|
| Calendar CalDAV (`ewscaldav.py`) | `17081` | `--port N` (default `17081`; add it to the systemd `ExecStart`) |
| Address book LDAP (`ldapgal.py`) | `17089` | CLI argument (`ldapgal.py N`) or `cred.json` → `ldap_port` |

To change any port, update **all** of the following consistently:

1. The service config above (and the matching `systemd/*.service` `ExecStart` for
   `ewscaldav` / `ldapgal`).
2. Thunderbird side (in `prefs.js`, or via the account / calendar / address-book
   dialogs):
   - `calendar.registry.<id>.uri` →
     `http://127.0.0.1:<caldav port>/dav/<user>/exchange/`
   - `ldap_2.servers.<name>.uri` →
     `ldap://127.0.0.1:<ldap port>/<base>??sub?(objectclass=*)`

Close Thunderbird before editing `prefs.js`, otherwise the changes are
overwritten on exit.

## 5. Known Limitations

- **Calendar attachments: TB's front-end is incomplete.** The event dialog's
  attachment menu only offers "URL" (`chrome/calendar/content/calendar-event-dialog.xhtml` has only `cmd_attach_url`, plus a disabled `cmd_attach_cloud` placeholder). Local-file attachments therefore cannot be added in TB; the bridge's file-attachment (base64) code is kept but not triggered by TB. It will work once upstream completes the UI.
- **Duplicate events from accepted invitations**: Exchange 2010 calendar items
  do not expose an iCalendar UID via the standard fields, so the bridge assigns
  each event a random UUID; when the user *accepts* an invitation from mail, TB
  creates a local event with the invitation's original UID → the same meeting
  appears twice. **Not fixed yet**; a viable fix is to read the meeting's
  `PidLidGlobalObjectId` extended property as the UID (verified readable and
  matching the invitation UID). See the Roadmap.
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
- `FindItem` returns no Body/structured attendees: bodies are filled via a
  batch `GetItem(IdOnly+item:Body)`, attendees come from the local mapping
  store — avoiding per-event `GetItem` that would slow down sync.
- **Credential safety**: the password lives only in
  `~/.config/ews-bridge/cred.json` (mode 600).

## 6. Roadmap

### Done

- **M0 Mail bridge**: Thunderbird's native EWS requests were relayed by the local
  bridge, which performed the NTLM handshake and rewrote request bodies
  (`archive`→`inbox`, drops `InternetMessageId`) to satisfy EWS 2010, injected
  missing ChangeKeys for write operations, repaired inline images on
  forward/reply (`x-moz-ews://` → `cid:` + `multipart/related`), with
  per-operation logging. **Now replaced by native IMAP/SMTP.**
- **M1 Read-only calendar**: CalDAV discovery (OPTIONS/PROPFIND hierarchy),
  `calendar-multiget`, `calendar-query(time-range)`, single/full GET; EWS
  `FindItem+CalendarView` (±180 days, ≤1200) → ICS; `uid ↔ ItemId/ChangeKey`
  stored in SQLite.
- **M2 Calendar write-back**: TB create/edit/delete → `PUT`/`DELETE` → EWS
  `CreateItem`/`UpdateItem`/`DeleteItem`; handles schema element order, FieldURI
  namespaces, and the mandatory `ChangeKey`.
- **M3 Meeting invitations**: parse `ATTENDEE` (required/optional) → EWS
  attendees; create/update/delete use `SendToAllAndSaveCopy` /
  `SendToChangedAndSaveCopy` / `SendToAllAndSaveCopy` (cancel); the read side
  emits `ORGANIZER`/`ATTENDEE` so TB shows participants correctly.
- **M4 Read-only GAL**: read-only LDAP v3 (Bind/Search + BER codec); filter
  extraction → EWS `ResolveNames`; 90s cache, ≤50 per query; verified with the
  TB address book search and compose-time autocomplete.
- **M5 Hardening**: normalize timezones to UTC; URI attachments (body marker ↔
  `ATTACH`); advertise calendar read/write privileges (otherwise TB forces the
  calendar read-only on every start); sync performance (batch body fetch, no
  per-event `GetItem`).

### TODO

- **M-UID alignment** (no external prerequisite; can be implemented now)
  - **Goal**: remove calendar duplicates caused by *accepting* invitations (the
    same meeting showing as two entries).
  - **Evidence**: Exchange 2010 does not expose `GlobalObjectId` via the
    standard fields, but the **extended property**
    `DistinguishedPropertySetId=Meeting, PropertyId=3, PropertyType=Binary`
    returns `PidLidGlobalObjectId` (base64); its hex value is *exactly* the
    invitation's iCalendar UID — verified to match the TB-side event's `uid`.
  - **Approach**: batch-`GetItem` this extended property → use it as the CalDAV
    UID (instead of a random UUID) → events TB created from the invitation and
    the EWS meeting merge automatically; migrate the local `uid↔itemid` map.
  - **Prerequisite**: none (capability already available); only implementation
    plus a one-time UID migration.
- **Local-file attachments** (waiting on upstream)
  - **Blocked by**: the TB event dialog offers only URL attachments
    (`chrome/calendar/content/calendar-event-dialog.xhtml` has only
    `cmd_attach_url`; `cmd_attach_cloud` is a disabled placeholder).
  - **Condition**: upstream completes the "local file" front-end. The bridge's
    base64 attachment path is already in place and needs almost no change.
- **CardDAV personal contacts** (waiting on data/API)
  - **Blocked by**: the Exchange 2010 personal Contacts folder is empty, and
    `ResolveNames` is search-only (not enumerable), which cannot support
    CardDAV's full-sync semantics.
  - **Condition**: revisit CardDAV once Contacts has data and an enumerable
    interface exists.

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