#!/usr/bin/env python3
# CalDAV server: exposes the Exchange 2010 calendar over EWS as a read-write
# read CalDAV calendar for Thunderbird. Bridge phase M1 (read-only).
import os, re, sqlite3, sys, time, uuid, argparse, json, html as _html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

import importlib.util
_bridge = os.environ.get("EWS_BRIDGE_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "ews_bridge.py")
_spec = importlib.util.spec_from_file_location("ewsb", _bridge)
b = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(b)

USER = b.load_conf().get("user", "") or "user@example.com"
LOG = open(b.load_conf().get("log", "/tmp/ews-bridge.log"), "a")

def elog(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    LOG.write(f"[{ts}] CALD {msg}\n")
    LOG.flush()

def x(s):
    from xml.sax.saxutils import escape
    return escape(s or "")

DB = os.path.expanduser(os.environ.get("EWS_CALDAV_DB", "~/.cache/ewscaldav.db"))
os.makedirs(os.path.dirname(DB), exist_ok=True)

def db():
    c = sqlite3.connect(DB)
    c.execute("CREATE TABLE IF NOT EXISTS ev(uid TEXT PRIMARY KEY, itemid TEXT UNIQUE, changekey TEXT, attendees TEXT)")
    try:
        c.execute("ALTER TABLE ev ADD COLUMN attendees TEXT")
    except sqlite3.OperationalError:
        pass
    return c

def clean_html(s):
    s = re.sub(r"<[^>]+>", "", s)
    return _html.unescape(s)

def event_has_attendees(event):
    return bool(event.get("required") or event.get("optional"))

def event_attendees(event):
    return {"required": event.get("required") or [],
            "optional": event.get("optional") or []}

def attendees_xml(event):
    """Build <t:RequiredAttendees>/<t:OptionalAttendees> for CalendarItem.
    EWS 2010 schema order: ..., Location, When(?), ..., Organizer,
    RequiredAttendees, OptionalAttendees, ..."""
    out = ""
    req = event.get("required") or []
    if req:
        out += "<t:RequiredAttendees>"
        for em in req:
            out += (f"<t:Attendee><t:Mailbox><t:EmailAddress>{x(em)}</t:EmailAddress>"
                    f"</t:Mailbox></t:Attendee>")
        out += "</t:RequiredAttendees>"
    opt = event.get("optional") or []
    if opt:
        out += "<t:OptionalAttendees>"
        for em in opt:
            out += (f"<t:Attendee><t:Mailbox><t:EmailAddress>{x(em)}</t:EmailAddress>"
                    f"</t:Mailbox></t:Attendee>")
        out += "</t:OptionalAttendees>"
    return out

def ews_find(start, end):
    body = f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"
 xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types"
 xmlns:m="http://schemas.microsoft.com/exchange/services/2006/messages">
<soap:Header><t:RequestServerVersion Version="Exchange2010_SP1"/></soap:Header><soap:Body>
<m:FindItem Traversal="Shallow"><m:ItemShape><t:BaseShape>AllProperties</t:BaseShape></m:ItemShape>
<m:CalendarView MaxEntriesReturned="1200" StartDate="{start}" EndDate="{end}"/>
<m:ParentFolderIds><t:DistinguishedFolderId Id="calendar"/></m:ParentFolderIds></m:FindItem></soap:Body></soap:Envelope>""".encode()
    hdr = [("Content-Type", "text/xml; charset=utf-8"),
           ("SOAPAction", "http://schemas.microsoft.com/exchange/services/2006/messages/FindItem")]
    items = []
    for attempt in range(5):
        code, _oh, out = b.ews_via_curl("/ews/exchange.asmx", body, hdr)
        txt = out.decode("utf-8", "replace")
        items = []
        if code == 200 and b"Fault" not in out[:500]:
            for it in re.findall(r"<t:CalendarItem[ >].*?</t:CalendarItem>", txt, re.S):
                def g(tag):
                    m = re.search(rf"<t:{tag}[^>]*>(.*?)</t:{tag}>", it, re.S)
                    return m.group(1).strip() if m else ""
                iid = re.search(r"<t:ItemId Id=\"([^\"]+)\" ChangeKey=\"([^\"]+)\"", it)
                if not iid:
                    continue
                att_ids = re.findall(r'<t:AttachmentId Id="([^"]+)"', it)
                items.append(dict(itemid=iid.group(1), changekey=iid.group(2),
                                  subject=clean_html(g("Subject")), start=g("Start"), end=g("End"),
                                  isallday=g("IsAllDayEvent").lower() == "true",
                                  location=clean_html(g("Location")),
                                  organizer=g("DisplayTo"), body=clean_html(g("Body")),
                                  attachment_ids=att_ids))
        if items:
            return items
        elog(f"FIND attempt {attempt+1}/5 empty (code={code}) range={start}..{end} retrying")
        time.sleep(1.0)
    return items

def _body_with_uris(event, current=None):
    """Return a body that also carries URI-only attachments as plain-text
    lines (EWS 2010 <t:Body> content model is plain-text/TEXT; it cannot hold
    HTML fragments like <br/>). Existing markers already in `current` (the EWS
    body) are carried over so a client that drops the ATTACH row doesn't lose
    the URL. Idempotent: a URI present anywhere in the result is not re-added."""
    body = event.get("body") or ""
    uris = [a["uri"] for a in (event.get("attachments") or []) if a.get("uri")]
    _, cur_uris = _split_body_markers(current or "")
    ordered = []
    for u in uris + cur_uris:
        if u not in ordered:
            ordered.append(u)
    result = body
    for u in ordered:
        if u not in result:
            result += "\n附件: " + u
    return result

def attachments_xml(event, only=None):
    """Build <t:Attachments> for CreateItem. Must come after <t:Body> and before start fields."""
    atts = event.get("attachments") or []
    if only is not None:
        atts = [a for i, a in enumerate(atts) if i in only]
    if not atts:
        return ""
    out = "<t:Attachments>"
    for i, a in enumerate(atts):
        if a.get("base64") is None:
            continue
        out += (f'<t:FileAttachment><t:Name>{x(a.get("name") or "attachment")}</t:Name>'
                f'<t:ContentType>{x(a.get("ctype") or "application/octet-stream")}</t:ContentType>'
                f'<t:IsInline>false</t:IsInline>'
                f'<t:Content>{a["base64"]}</t:Content></t:FileAttachment>')
    out += "</t:Attachments>"
    if out == "<t:Attachments></t:Attachments>":
        return ""
    return out

def ews_create(event):
    send = "SendToAllAndSaveCopy" if event_has_attendees(event) else "SendToNone"
    body_html = _body_with_uris(event)
    body = f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"
 xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types"
 xmlns:m="http://schemas.microsoft.com/exchange/services/2006/messages">
<soap:Header><t:RequestServerVersion Version="Exchange2010_SP1"/></soap:Header><soap:Body>
<m:CreateItem SendMeetingInvitations="{send}"><m:Items>
<t:CalendarItem><t:Subject>{x(event.get('subject'))}</t:Subject>
<t:Body BodyType="Text">{x(body_html)}</t:Body>
{attachments_xml(event)}
<t:Start>{event.get('start')}</t:Start><t:End>{event.get('end')}</t:End>
<t:Location>{x(event.get('location'))}</t:Location>{attendees_xml(event)}</t:CalendarItem>
</m:Items></m:CreateItem></soap:Body></soap:Envelope>""".encode()
    hdr = [("Content-Type", "text/xml; charset=utf-8"),
           ("SOAPAction", "http://schemas.microsoft.com/exchange/services/2006/messages/CreateItem")]
    for attempt in range(5):
        code, _oh, out = b.ews_via_curl("/ews/exchange.asmx", body, hdr)
        txt = out.decode("utf-8", "replace")
        if code == 200 and b"Fault" not in out[:500]:
            it = re.search(r"<t:ItemId Id=\"([^\"]+)\" ChangeKey=\"([^\"]+)\"", txt)
            if it:
                return dict(itemid=it.group(1), changekey=it.group(2))
            elog(f"CREATE empty itemid code={code} retrying")
            time.sleep(1.0)
            continue
        if "ErrorCreateItemAccessDenied" in txt:
            return None
        elog(f"CREATE fail attempt={attempt+1} code={code}: {txt[:200]}")
        time.sleep(1.0)
    return None

def ews_attachments_delete(attachment_ids):
    """Delete remote attachments by their EWS AttachmentId."""
    if not attachment_ids:
        return True
    ids = "".join(f'<t:AttachmentId Id="{i}"/>' for i in attachment_ids)
    body = f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"
 xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types"
 xmlns:m="http://schemas.microsoft.com/exchange/services/2006/messages">
<soap:Header><t:RequestServerVersion Version="Exchange2010_SP1"/></soap:Header><soap:Body>
<m:DeleteAttachment><m:AttachmentIds>{ids}</m:AttachmentIds></m:DeleteAttachment></soap:Body></soap:Envelope>""".encode()
    hdr = [("Content-Type", "text/xml; charset=utf-8"),
           ("SOAPAction", "http://schemas.microsoft.com/exchange/services/2006/messages/DeleteAttachment")]
    for attempt in range(5):
        code, _oh, out = b.ews_via_curl("/ews/exchange.asmx", body, hdr)
        if code == 200 and b"Fault" not in out[:500]:
            return True
        elog(f"DELATT fail attempt={attempt+1} code={code}: {out[:200]!r}")
        time.sleep(1.0)
    return False

def ews_item_body(itemid):
    """Fetch current Body HTML of an EWS item (needed to make URI-attachment
    links idempotent across updates)."""
    body = f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"
 xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types"
 xmlns:m="http://schemas.microsoft.com/exchange/services/2006/messages">
<soap:Header><t:RequestServerVersion Version="Exchange2010_SP1"/></soap:Header><soap:Body>
<m:GetItem><m:ItemShape><t:BaseShape>AllProperties</t:BaseShape></m:ItemShape>
<m:ItemIds><t:ItemId Id="{itemid}"/></m:ItemIds></m:GetItem></soap:Body></soap:Envelope>""".encode()
    hdr = [("Content-Type", "text/xml; charset=utf-8"),
           ("SOAPAction", "http://schemas.microsoft.com/exchange/services/2006/messages/GetItem")]
    out = b""
    for attempt in range(5):
        code, _oh, out = b.ews_via_curl("/ews/exchange.asmx", body, hdr)
        if code == 200:
            break
        elog(f"GETBODY attempt={attempt+1} code={code}: {out[:200]!r}")
        time.sleep(1.0)
    m = re.search(r"<t:Body[^>]*>(.*?)</t:Body>", out.decode("utf-8", "replace"), re.S)
    return m.group(1) if m else ""

def ews_attachments_meta(itemid):
    """Return list of {id, name, ctype} of existing EWS attachments via GetItem (AllProperties)."""
    body = f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"
 xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types"
 xmlns:m="http://schemas.microsoft.com/exchange/services/2006/messages">
<soap:Header><t:RequestServerVersion Version="Exchange2010_SP1"/></soap:Header><soap:Body>
<m:GetItem><m:ItemShape><t:BaseShape>AllProperties</t:BaseShape></m:ItemShape>
<m:ItemIds><t:ItemId Id="{itemid}"/></m:ItemIds></m:GetItem></soap:Body></soap:Envelope>""".encode()
    hdr = [("Content-Type", "text/xml; charset=utf-8"),
           ("SOAPAction", "http://schemas.microsoft.com/exchange/services/2006/messages/GetItem")]
    out = b""
    for attempt in range(5):
        code, _oh, out = b.ews_via_curl("/ews/exchange.asmx", body, hdr)
        if code == 200:
            break
        elog(f"GETATT-meta attempt={attempt+1} code={code}: {out[:200]!r}")
        time.sleep(1.0)
    txt = out.decode("utf-8", "replace")
    ids = re.findall(r"<t:AttachmentId Id=\"([^\"]+)\"", txt)
    names = re.findall(r"<t:Name>([^<]*)</t:Name>", txt)
    ct = re.findall(r"<t:ContentType>([^<]*)</t:ContentType>", txt)
    return [dict(id=ids[i], name=names[i] if i < len(names) else "",
                 ctype=ct[i] if i < len(ct) else "") for i in range(len(ids))]

def ews_attachments_add(itemid, event):
    """Add all base64 attachments from event to the remote item (CreateAttachment)."""
    atts = [a for a in (event.get("attachments") or []) if a.get("base64") is not None]
    if not atts:
        return True
    fatts = "".join(
        f'<t:FileAttachment><t:Name>{x(a.get("name") or "attachment")}</t:Name>'
        f'<t:ContentType>{x(a.get("ctype") or "application/octet-stream")}</t:ContentType>'
        f'<t:IsInline>false</t:IsInline><t:Content>{a["base64"]}</t:Content></t:FileAttachment>'
        for a in atts)
    body = f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"
 xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types"
 xmlns:m="http://schemas.microsoft.com/exchange/services/2006/messages">
<soap:Header><t:RequestServerVersion Version="Exchange2010_SP1"/></soap:Header><soap:Body>
<m:CreateAttachment ParentItemId="{itemid}"><m:Attachments>{fatts}</m:Attachments></m:CreateAttachment></soap:Body></soap:Envelope>""".encode()
    hdr = [("Content-Type", "text/xml; charset=utf-8"),
           ("SOAPAction", "http://schemas.microsoft.com/exchange/services/2006/messages/CreateAttachment")]
    for attempt in range(5):
        code, _oh, out = b.ews_via_curl("/ews/exchange.asmx", body, hdr)
        if code == 200 and b"Fault" not in out[:500]:
            return True
        elog(f"ADDATT fail attempt={attempt+1} code={code}: {out[:200]!r}")
        time.sleep(1.0)
    return False

def ews_update(event, itemid, changekey=None):
    if not changekey:
        ck = db().execute("SELECT changekey FROM ev WHERE itemid=?", (itemid,)).fetchone()
        changekey = ck[0] if ck else None
    if not changekey:
        changekey = _fetch_changekey(itemid)
    if not changekey:
        elog(f"UPDATE no-changekey itemid={itemid[:40]}")
        return False
    iid = f'<t:ItemId Id="{itemid}" ChangeKey="{changekey}"/>'
    send = "SendToChangedAndSaveCopy" if event_has_attendees(event) else "SendToNone"
    current_body = ""
    if event.get("attachments"):
        try:
            current_body = ews_item_body(itemid)
        except Exception:
            current_body = ""
    body_with_uris = _body_with_uris(event, current_body)
    att_fields = ""
    if event.get("required"):
        att_fields += (f'<t:SetItemField><t:FieldURI FieldURI="calendar:RequiredAttendees"/><t:CalendarItem>'
                       f'<t:RequiredAttendees>{"".join(f"<t:Attendee><t:Mailbox><t:EmailAddress>{x(em)}</t:EmailAddress></t:Mailbox></t:Attendee>" for em in event["required"])}'
                       f'</t:RequiredAttendees></t:CalendarItem></t:SetItemField>')
    if event.get("optional"):
        att_fields += (f'<t:SetItemField><t:FieldURI FieldURI="calendar:OptionalAttendees"/><t:CalendarItem>'
                       f'<t:OptionalAttendees>{"".join(f"<t:Attendee><t:Mailbox><t:EmailAddress>{x(em)}</t:EmailAddress></t:Mailbox></t:Attendee>" for em in event["optional"])}'
                       f'</t:OptionalAttendees></t:CalendarItem></t:SetItemField>')
    body = f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"
 xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types"
 xmlns:m="http://schemas.microsoft.com/exchange/services/2006/messages">
<soap:Header><t:RequestServerVersion Version="Exchange2010_SP1"/></soap:Header><soap:Body>
<m:UpdateItem ConflictResolution="AlwaysOverwrite" MessageDisposition="SaveOnly" SendMeetingInvitationsOrCancellations="{send}"><m:ItemChanges><t:ItemChange>
{iid}<t:Updates><t:SetItemField><t:FieldURI FieldURI="item:Subject"/><t:CalendarItem><t:Subject>{x(event.get('subject'))}</t:Subject></t:CalendarItem></t:SetItemField>
<t:SetItemField><t:FieldURI FieldURI="calendar:Start"/><t:CalendarItem><t:Start>{event.get('start')}</t:Start></t:CalendarItem></t:SetItemField>
<t:SetItemField><t:FieldURI FieldURI="calendar:End"/><t:CalendarItem><t:End>{event.get('end')}</t:End></t:CalendarItem></t:SetItemField>
<t:SetItemField><t:FieldURI FieldURI="calendar:Location"/><t:CalendarItem><t:Location>{x(event.get('location'))}</t:Location></t:CalendarItem></t:SetItemField>
<t:SetItemField><t:FieldURI FieldURI="item:Body"/><t:CalendarItem><t:Body BodyType="Text">{x(body_with_uris)}</t:Body></t:CalendarItem></t:SetItemField>
{att_fields}
</t:Updates></t:ItemChange></m:ItemChanges></m:UpdateItem></soap:Body></soap:Envelope>""".encode()
    hdr = [("Content-Type", "text/xml; charset=utf-8"),
           ("SOAPAction", "http://schemas.microsoft.com/exchange/services/2006/messages/UpdateItem")]
    for attempt in range(5):
        code, _oh, out = b.ews_via_curl("/ews/exchange.asmx", body, hdr)
        if code == 200 and b"Fault" not in out[:500]:
            sync_attachments(itemid, event)
            return True
        elog(f"UPDATE fail attempt={attempt+1} code={code}: {out[:200]!r}")
        time.sleep(1.0)
    return False

_ATT_CONTENT_CACHE = {}

def ews_attachment_content(attachment_ids):
    """Returns list of {name, ctype, base64, uri} for attachment ids via
    GetAttachment. Cached in memory (TBs rarely re-read the same event)."""
    if not attachment_ids:
        return []
    key = "|".join(attachment_ids)
    if key in _ATT_CONTENT_CACHE:
        return _ATT_CONTENT_CACHE[key]
    ids = "".join(f'<t:AttachmentId Id="{i}"/>' for i in attachment_ids)
    body = f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"
 xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types"
 xmlns:m="http://schemas.microsoft.com/exchange/services/2006/messages">
<soap:Header><t:RequestServerVersion Version="Exchange2010_SP1"/></soap:Header><soap:Body>
<m:GetAttachment><m:AttachmentShape><t:IncludeMimeContent>false</t:IncludeMimeContent></m:AttachmentShape>
<m:AttachmentIds>{ids}</m:AttachmentIds></m:GetAttachment></soap:Body></soap:Envelope>""".encode()
    hdr = [("Content-Type", "text/xml; charset=utf-8"),
           ("SOAPAction", "http://schemas.microsoft.com/exchange/services/2006/messages/GetAttachment")]
    out = b""
    for attempt in range(5):
        code, _oh, out = b.ews_via_curl("/ews/exchange.asmx", body, hdr)
        if code == 200:
            break
        elog(f"GETATT attempt={attempt+1} code={code}: {out[:200]!r}")
        time.sleep(1.0)
    txt = out.decode("utf-8", "replace")
    res = []
    for fa in re.findall(r"<t:FileAttachment>(.*?)</t:FileAttachment>", txt, re.S):
        name = re.search(r"<t:Name>([^<]*)</t:Name>", fa)
        ct = re.search(r"<t:ContentType>([^<]*)</t:ContentType>", fa)
        content = re.search(r"<t:Content[^>]*>(.*?)</t:Content>", fa, re.S)
        res.append(dict(name=(name.group(1) if name else ""),
                        ctype=(ct.group(1) if ct else ""),
                        base64=(content.group(1).strip() if content else None),
                        uri=None))
    _ATT_CONTENT_CACHE[key] = res
    return res

_BODY_CACHE = {}

def _lazy_item_full(it):
    """GetItem an item's Body + (non-inline) attachment ids. FindItem never
    returns Body, so this runs only for single-event GETs. Cached; a batch
    body-only entry (attachment_ids=None) is upgraded to a full fetch here."""
    iid = it.get("itemid")
    cached = _BODY_CACHE.get(iid)
    if cached and cached.get("attachment_ids") is not None:
        return cached
    body = f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"
 xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types"
 xmlns:m="http://schemas.microsoft.com/exchange/services/2006/messages">
<soap:Header><t:RequestServerVersion Version="Exchange2010_SP1"/></soap:Header><soap:Body>
<m:GetItem><m:ItemShape><t:BaseShape>AllProperties</t:BaseShape></m:ItemShape>
<m:ItemIds><t:ItemId Id="{iid}"/></m:ItemIds></m:GetItem></soap:Body></soap:Envelope>""".encode()
    hdr = [("Content-Type", "text/xml; charset=utf-8"),
           ("SOAPAction", "http://schemas.microsoft.com/exchange/services/2006/messages/GetItem")]
    out = b""
    for attempt in range(5):
        code, _oh, out = b.ews_via_curl("/ews/exchange.asmx", body, hdr)
        if code == 200:
            break
        elog(f"LAZYFULL attempt={attempt+1} code={code}: {out[:120]!r}")
        time.sleep(1.0)
    txt = out.decode("utf-8", "replace")
    m = re.search(r"<t:Body[^>]*>(.*?)</t:Body>", txt, re.S)
    btxt = clean_html(m.group(1)) if m else ""
    nids = []
    for fa in re.findall(r"<t:FileAttachment>(.*?)</t:FileAttachment>", txt, re.S):
        isin = re.search(r"<t:IsInline>(true|false)</t:IsInline>", fa)
        if isin and isin.group(1) == "true":
            continue
        aid = re.search(r'<t:AttachmentId Id="([^"]+)"', fa)
        if aid:
            nids.append(aid.group(1))
    _BODY_CACHE[iid] = dict(body=btxt, attachment_ids=nids)
    return _BODY_CACHE[iid]

def ews_bodies_batch(items):
    """Body-only GetItem for many items, chunked. Used by the bulk calendar
    serialization so TB still sees DESCRIPTION / URI-attachment markers
    without per-item GetItem latency or inline-image bloat."""
    ids = [x["itemid"] for x in items if x.get("itemid")]
    CH = 50
    for i in range(0, len(ids), CH):
        chunk = ids[i:i + CH]
        req_ids = "".join(f'<t:ItemId Id="{x}"/>' for x in chunk)
        body = f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"
 xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types"
 xmlns:m="http://schemas.microsoft.com/exchange/services/2006/messages">
<soap:Header><t:RequestServerVersion Version="Exchange2010_SP1"/></soap:Header><soap:Body>
<m:GetItem><m:ItemShape><t:BaseShape>IdOnly</t:BaseShape>
<t:AdditionalProperties><t:FieldURI FieldURI="item:Body"/></t:AdditionalProperties>
</m:ItemShape><m:ItemIds>{req_ids}</m:ItemIds></m:GetItem></soap:Body></soap:Envelope>""".encode()
        hdr = [("Content-Type", "text/xml; charset=utf-8"),
               ("SOAPAction", "http://schemas.microsoft.com/exchange/services/2006/messages/GetItem")]
        out = b""
        for attempt in range(5):
            code, _oh, out = b.ews_via_curl("/ews/exchange.asmx", body, hdr)
            if code == 200:
                break
            elog(f"BATCHBODY attempt={attempt+1} code={code}: {out[:120]!r}")
            time.sleep(1.0)
        txt = out.decode("utf-8", "replace")
        for blk in re.findall(r"<t:CalendarItem>(.*?)</t:CalendarItem>", txt, re.S):
            iid = re.search(r'<t:ItemId Id="([^"]+)"', blk)
            bm = re.search(r"<t:Body[^>]*>(.*?)</t:Body>", blk, re.S)
            if iid:
                _BODY_CACHE.setdefault(iid.group(1), dict(
                    body=(clean_html(bm.group(1)) if bm else ""), attachment_ids=None))

def sync_attachments(itemid, event):
    """Reconcile EWS attachments with the event's attachments.
    UpdateItem cannot touch attachments, so we use DeleteAttachment/
    CreateAttachment when the client actually sent ATTACH lines. If the PUT
    payload had no ATTACH at all (TB kept existing attachments), we leave the
    remote list untouched to avoid data loss."""
    if not event.get("saw_attach"):
        return
    want = [a for a in (event.get("attachments") or []) if a.get("base64") is not None]
    remote = []
    try:
        remote = ews_attachments_meta(itemid)
    except Exception as e:
        elog(f"SYNCATT meta fail: {e}")
        return
    rem_ids = [r["id"] for r in remote]
    if rem_ids:
        if not want:
            ews_attachments_delete(rem_ids)
            return
        # cheap check: single attachment kept unchanged -> skip churn
        if len(rem_ids) == 1 and len(want) == 1:
            return
        ews_attachments_delete(rem_ids)
        ews_attachments_add(itemid, event)
    elif want:
        ews_attachments_add(itemid, event)

def ews_delete(itemid):
    send = "SendToNone"
    row = db().execute("SELECT attendees FROM ev WHERE itemid=?", (itemid,)).fetchone()
    if row and row[0]:
        try:
            atts = json.loads(row[0])
            if atts and (atts.get("required") or atts.get("optional")):
                send = "SendToAllAndSaveCopy"
        except Exception:
            pass
    body = f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"
 xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types"
 xmlns:m="http://schemas.microsoft.com/exchange/services/2006/messages">
<soap:Header><t:RequestServerVersion Version="Exchange2010_SP1"/></soap:Header><soap:Body>
<m:DeleteItem DeleteType="HardDelete" SendMeetingCancellations="{send}"><m:ItemIds><t:ItemId Id="{itemid}"/></m:ItemIds></m:DeleteItem></soap:Body></soap:Envelope>""".encode()
    hdr = [("Content-Type", "text/xml; charset=utf-8"),
           ("SOAPAction", "http://schemas.microsoft.com/exchange/services/2006/messages/DeleteItem")]
    for attempt in range(5):
        code, _oh, out = b.ews_via_curl("/ews/exchange.asmx", body, hdr)
        if code == 200 and b"Fault" not in out[:500]:
            return True
        elog(f"DELETE fail attempt={attempt+1} code={code}: {out[:200]!r}")
        time.sleep(1.0)
    return False

def _split_ic(line):
    """Parse 'NAME;P1=..;P2=..:value' handling colons inside quoted
    parameter values (e.g. ALTREP="data:text/html,..."). Returns
    (params, value). Cut at the FIRST ':' not inside double quotes, so
    URI values like 'https://x/y' (which contain colons) stay intact."""
    inq = False
    cut = -1
    for i in range(len(line)):
        ch = line[i]
        if ch == '"':
            inq = not inq
        elif ch == ':' and not inq:
            cut = i
            break
    if cut < 0:
        return "", ""
    return line[:cut], line[cut + 1:].strip()

def parse_ics(data):
    """Parse a minimal VCALENDAR/VEVENT payload from Thunderbird PUT."""
    ev = dict(subject="", start=None, end=None, location="", body="", isallday=False,
              required=[], optional=[], attachments=[], saw_attach=False)
    text = data.decode("utf-8", "replace")
    ve = re.search(r"BEGIN:VEVENT.*?END:VEVENT", text, re.S)
    if not ve:
        return None
    v = ve.group(0)

    unfolded = re.sub(r"\r?\n[ \t]", "", v)

    def attendee_list(propname):
        out = []
        for m in re.finditer(rf"^{propname}(.*)$", unfolded, re.M | re.I):
            params, raw = _split_ic(m.group(1))
            raw = raw.strip()
            role = re.search(r"ROLE=([^;:]+)", params, re.I)
            is_opt = role and role.group(1).upper() in ("OPT-PARTICIPANT",)
            if propname.upper() == "OPTIONAL-ATTENDEE":
                is_opt = True
            raw = re.sub(r"^mailto:", "", raw, flags=re.I)
            if raw:
                out.append((raw, is_opt))
        return out

    atts = attendee_list("ATTENDEE") + attendee_list("OPTIONAL-ATTENDEE")
    ev["required"] = [em for em, opt in atts if not opt]
    ev["optional"] = [em for em, opt in atts if opt]

    def prop(name):
        m = re.search(rf"^{name}(.*)$", unfolded, re.M | re.I)
        if not m:
            return ""
        _p, val = _split_ic(m.group(1))
        return val

    def prop_full(name):
        m = re.search(rf"^{name}(.*)$", unfolded, re.M | re.I)
        if not m:
            return ("", "")
        p, val = _split_ic(m.group(1))
        return (p, val)

    uid = re.search(r"^UID:(.*)$", unfolded, re.M | re.I)
    ev["uid"] = uid.group(1).strip() if uid else str(uuid.uuid4())

    ev["subject"] = clean_html(prop("SUMMARY"))
    loc = prop("LOCATION").replace("\\,", ",").replace("\\n", "\n")
    ev["location"] = loc

    dstart = prop("DTSTART")
    dend = prop("DTEND")
    ev["isallday"] = dstart.startswith("VALUE=DATE") or re.match(r"^\d{8}$", dstart) is not None

    desc = prop("DESCRIPTION").replace("\\n", "\n").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")
    ev["body"] = clean_html(desc)

    # Normalize floating/UTC times to Exchange ISO-8601 (UTC). Exchange wants Z.
    # TB (Asia/Shanghai) sends floating local times without Z/TZID. Treat as
    # local Japan/Shanghai (+08:00) and convert to UTC, unless already marked Z
    # or carrying an explicit TZID (which TB resolves to the right wall time).
    LOCAL_OFFSET = time.timezone if time.localtime().tm_isdst == 0 else time.timezone * -1
    def norm(ts, tzid=None):
        if not ts:
            return None
        if re.match(r"^\d{8}$", ts):
            return ts  # all-day date, keep as-is
        is_utc = ts.endswith("Z")
        ts = ts.replace("Z", "")
        m = re.match(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})$", ts)
        if not m:
            return ts + ("Z" if is_utc else "")
        y, mo, d, h, mi, s = m.groups()
        t = time.mktime((int(y), int(mo), int(d), int(h), int(mi), int(s), 0, 0, -1))
        if is_utc:
            return f"{y}-{mo}-{d}T{h}:{mi}:{s}Z"
        # 本地时间：Asia/Shanghai == UTC+8, no DST
        local = time.localtime(t)
        off = time.mktime(local) - time.mktime(time.gmtime(t))
        import datetime as _dt
        base = _dt.datetime(int(y), int(mo), int(d), int(h), int(mi), int(s))
        u = base - _dt.timedelta(seconds=off)
        return u.strftime("%Y-%m-%dT%H:%M:%SZ")

    tp, ts_ = prop_full("DTSTART")
    tzid = re.search(r"TZID=([^;:]+)", tp, re.I)
    ev["start"] = norm(ts_, tzid.group(1) if tzid else None) or None
    tp2, ts2 = prop_full("DTEND")
    tzid2 = re.search(r"TZID=([^;:]+)", tp2, re.I)
    ev["end"] = norm(ts2, tzid2.group(1) if tzid2 else None) or None

    atts_out = []
    for am in re.finditer(r"^ATTACH(.*)$", unfolded, re.M | re.I):
        ev["saw_attach"] = True
        params, raw = _split_ic(am.group(1))
        raw = raw.strip()
        name = ""
        ctype = ""
        fmt = re.search(r"FMTTYPE=([^;:]+)", params, re.I)
        if fmt:
            ctype = fmt.group(1).split('"')[0].strip()
        nm = re.search(r"FILENAME(?:=|;\s*=)([^;:\r\n]+)", params, re.I)
        if nm:
            name = nm.group(1).strip()
            if name.startswith('"') and name.endswith('"'):
                name = name[1:-1]
        is_binary = "ENCODING=BASE64" in params.upper() or "VALUE=BINARY" in params.upper()
        if is_binary or not re.match(r"^[a-z][a-z0-9+.-]*:", raw, re.I):
            b64 = re.sub(r"\s+", "", raw)
            atts_out.append(dict(name=name or "attachment", ctype=ctype or "application/octet-stream",
                                 base64=b64, uri=None))
        else:
            atts_out.append(dict(name=name or raw, ctype=ctype or "application/octet-stream",
                                 base64=None, uri=raw))
    ev["attachments"] = atts_out
    return ev

def _fetch_changekey(itemid):
    """Return the newest ChangeKey for an EWS item by re-FindItem; fallback to cached."""
    try:
        st = time.strftime("%Y-%m-%dT00:00:00Z", time.gmtime(time.time() - 180 * 86400))
        en = time.strftime("%Y-%m-%dT00:00:00Z", time.gmtime(time.time() + 180 * 86400))
        for it in ews_find(st, en):
            if it["itemid"] == itemid:
                return it["changekey"]
    except Exception as e:
        elog(f"changekey refetch fail: {e}")
    return None

_CACHE = {}
_CK_SEEN = {}

def build_event_list(start, end):
    items = ews_find(start, end)
    for it in items:
        iid, ck = it["itemid"], it.get("changekey")
        if _CK_SEEN.get(iid) not in (None, ck):
            _BODY_CACHE.pop(iid, None)
        _CK_SEEN[iid] = ck
    need = [it for it in items if it.get("itemid") not in _BODY_CACHE]
    if need:
        try:
            ews_bodies_batch(need)
        except Exception as e:
            elog(f"BATCHBODY fail: {e}")
    c = db()
    out = []
    for it in items:
        row = c.execute("SELECT uid, attendees FROM ev WHERE itemid=?", (it["itemid"],)).fetchone()
        if row:
            uid = row[0]
            c.execute("UPDATE ev SET changekey=? WHERE itemid=?", (it["changekey"], it["itemid"]))
            try:
                att = json.loads(row[1]) if row[1] else {}
            except Exception:
                att = {}
        else:
            uid = str(uuid.uuid4())
            c.execute("INSERT INTO ev(uid,itemid,changekey) VALUES(?,?,?)",
                      (uid, it["itemid"], it["changekey"]))
            att = {}
        it["required"] = att.get("required") or []
        it["optional"] = att.get("optional") or []
        cached = _BODY_CACHE.get(it["itemid"]) or {}
        if cached.get("body"):
            it["body"] = cached["body"]
        else:
            it["body"] = it.get("body") or ""
        it["attachment_ids"] = cached.get("attachment_ids") or []
        c.commit()
        _CACHE[uid] = it
        out.append((uid, it))
    c.close()
    return out

def esc_txt(s):
    return (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r", "").replace("\n", "\\n")

def cal_fmt(ts):
    if len(ts) >= 19:
        r = re.sub(r"[-:]", "", ts)
        r = r.replace(".000", "").replace("Z", "Z")
        return r
    return ts

def all_day_end(it):
    import datetime
    d = datetime.date.fromisoformat(it["start"][:10])
    e = datetime.date.fromisoformat(it["end"][:10]) if it.get("end") and len(it["end"]) >= 10 else d
    if e <= d:
        e = d + datetime.timedelta(days=1)
    return e.isoformat().replace("-", "")

def _split_body_markers(body):
    """Split our persisted URI-attachment lines ("附件: <url>") out of the
    plain-text body so to_ics can re-emit them as real ATTACH rows. Returns
    (body_without_markers, [uri,...])."""
    uris = []
    out = []
    for ln in re.split(r"\r?\n", body or ""):
        m = re.match(r"^\s*附件[:：]\s*(\S+)\s*$", ln)
        if m and (m.group(1).startswith("http://") or m.group(1).startswith("https://")):
            uris.append(m.group(1))
        else:
            out.append(ln)
    return "\n".join(out).strip("\n"), uris

def to_ics(uid, it, full=False):
    # Bulk serialization relies on it["body"] populated by build_event_list
    # (batch GetItem). Only a single-event fetch pulls attachments via GetItem.
    if full and it.get("itemid"):
        try:
            f = _lazy_item_full(it)
            it["body"] = f["body"]
            it["attachment_ids"] = f["attachment_ids"] or []
            if f["attachment_ids"]:
                try:
                    it["attachments"] = ews_attachment_content(f["attachment_ids"])
                except Exception as e:
                    elog(f"LAZYATT fail {it['itemid'][:20]}: {e}")
                    it["attachments"] = []
        except Exception as e:
            elog(f"LAZYFULL fail itemid={it.get('itemid','')[:20]}: {e}")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0",
             "PRODID:-//EWS Bridge//Exchange 2010//ZH-CN", "CALSCALE:GREGORIAN",
             "BEGIN:VEVENT", f"UID:{uid}"]
    lines.append("DTSTAMP:" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
    if it.get("isallday"):
        lines.append("DTSTART;VALUE=DATE:" + it["start"][:10].replace("-", ""))
        lines.append("DTEND;VALUE=DATE:" + all_day_end(it))
    else:
        lines.append("DTSTART:" + cal_fmt(it["start"]))
        lines.append("DTEND:" + cal_fmt(it.get("end", it["start"])))
    s = esc_txt(it.get("subject"))
    if s:
        lines.append(f"SUMMARY:{s}")
    loc = esc_txt(it.get("location"))
    if loc:
        lines.append(f"LOCATION:{loc}")
    req = it.get("required") or []
    opt = it.get("optional") or []
    if req or opt:
        if USER:
            lines.append(f"ORGANIZER;CN={esc_txt(USER)}:mailto:{USER}")
        for em in req:
            lines.append(f"ATTENDEE;PARTSTAT=NEEDS-ACTION;ROLE=REQ-PARTICIPANT;CN={esc_txt(em)}:mailto:{em}")
        for em in opt:
            lines.append(f"ATTENDEE;PARTSTAT=NEEDS-ACTION;ROLE=OPT-PARTICIPANT;CN={esc_txt(em)}:mailto:{em}")
    body_text, marker_uris = _split_body_markers(clean_html(it.get("body") or ""))
    if body_text:
        lines.append("DESCRIPTION:" + esc_txt(body_text))
    seen = set()
    for a in it.get("attachments") or []:
        if a.get("base64"):
            lines.append(f"ATTACH;VALUE=BINARY;ENCODING=BASE64;FMTTYPE={a['ctype']};"
                         f"FILENAME={esc_txt(a['name'])}:{a['base64']}")
        elif a.get("uri"):
            lines.append(f"ATTACH;FMTTYPE={a['ctype']};FILENAME={esc_txt(a['name'])}:{a['uri']}")
            seen.add(a["uri"])
    for u in marker_uris:
        if u not in seen:
            lines.append(f"ATTACH:{u}")
    lines.append("STATUS:CONFIRMED")
    lines.append("TRANSP:OPAQUE")
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(lines) + "\r\n"

USER_H = USER
DAV = "/dav/"
HOME = f"/dav/{USER_H}/"
PRIN = f"/dav/principals/{USER_H}/"
CAL = f"/dav/{USER_H}/exchange/"

def etag(ck):
    # Bump READ_VER when the read-back serialization changes (e.g. adding
    # ATTENDEE/ORGANIZER) so CalDAV clients re-fetch unchanged events once.
    return f'"{ck}-r2"'

READ_VER = "r2"

def resp_headers(code, ctype="text/xml; charset=\"utf-8\"", body=b"", ct=None):
    from http.server import BaseHTTPRequestHandler as H
    h = {"Content-Type": ctype if ct is None else ct,
         "Content-Length": str(len(body)),
         "Connection": "close"}
    return h

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "EWSBridge/1.0"

    def log_message(self, *a):
        pass

    def _body(self):
        te = self.headers.get("Transfer-Encoding", "").lower()
        if te == "chunked":
            data = b""
            while True:
                line = self.rfile.readline()
                if not line:
                    break
                size = int(line.split(b";")[0].strip(), 16)
                if size <= 0:
                    while True:
                        t = self.rfile.readline()
                        if t in (b"\r\n", b"\n", b""):
                            break
                    break
                data += self.rfile.read(size)
                self.rfile.read(2)
            return data
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _logreq(self, tag=""):
        b = self._body() if self.command in ("PROPFIND", "REPORT") else b""
        elog(f"{self.command} {unquote(self.path)} depth={self.headers.get('Depth','-')} "
             f"len={len(b)} {tag} body={b[:220].decode('utf-8','replace')!r}")
        return b

    def _send(self, code, body=b"", ctype="text/xml; charset=\"utf-8\""):
        elog(f"=> {code} {len(body)}B for {self.command} {unquote(self.path)}")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _ms(self, items):
        body = ['<?xml version="1.0" encoding="utf-8"?>',
                '<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" '
                'xmlns:cs="http://calendarserver.org/ns/" xmlns:ical="http://apple.com/ns/ical/">']
        body.append("".join(items))
        body.append("</d:multistatus>")
        return "".join(body).encode("utf-8")

    def do_OPTIONS(self):
        self._logreq()
        self.send_response(200)
        self.send_header("DAV", "1, 2")
        self.send_header("Allow", "OPTIONS, GET, HEAD, POST, DELETE, PUT, PROPFIND, PROPPATCH, REPORT, MKCALENDAR")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---- discovery/hierarchy ------------------------------------------------
    def _root_propfind(self, depth):
        items = [
            f'<d:response><d:href>{DAV}</d:href><d:propstat><d:prop>'
            f'<d:resourcetype><d:collection/></d:resourcetype>'
            f'<c:calendar-home-set><d:href>{HOME}</d:href></c:calendar-home-set>'
            f'<d:current-user-principal><d:href>{PRIN}</d:href></d:current-user-principal>'
            f'</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>',
            f'<d:response><d:href>{PRIN}</d:href><d:propstat><d:prop>'
            f'<d:resourcetype><d:principal/></d:resourcetype>'
            f'<c:calendar-home-set><d:href>{HOME}</d:href></c:calendar-home-set>'
            f'</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>']
        if depth in ("1", "infinity"):
            items.append(self._home_response("1"))
        return self._ms(items)

    def _home_response(self, depth):
        r = (f'<d:response><d:href>{HOME}</d:href><d:propstat><d:prop>'
             f'<d:resourcetype><d:collection/></d:resourcetype>'
             f'<d:displayname>Exchange 日历（{USER}）</d:displayname>'
             f'<d:current-user-principal><d:href>{PRIN}</d:href></d:current-user-principal>'
             f'</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>')
        if depth == "1":
            r += (f'<d:response><d:href>{CAL}</d:href><d:propstat><d:prop>'
                  f'<d:resourcetype><d:collection/><c:calendar/></d:resourcetype>'
                  f'<d:displayname>Exchange日历</d:displayname>'
                  f'<c:supported-calendar-component-set><c:comp name="VEVENT"/></c:supported-calendar-component-set>'
                  f'<d:current-user-privilege-set><d:privilege><d:read/></d:privilege>'
                  f'<d:privilege><d:write/></d:privilege>'
                  f'<d:privilege><d:write-content/></d:privilege>'
                  f'<d:privilege><d:bind/></d:privilege>'
                  f'<d:privilege><d:unbind/></d:privilege>'
                  f'</d:current-user-privilege-set>'
                  f'</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>')
        return r

    _CAL_PROPS_BASIC = (f'<d:resourcetype><d:collection/><c:calendar/></d:resourcetype>'
                        f'<d:displayname>Exchange日历</d:displayname>'
                        f'<c:supported-calendar-component-set><c:comp name="VEVENT"/></c:supported-calendar-component-set>'
                        f'<ical:calendar-color>#0A84FF</ical:calendar-color>'
                        f'<d:current-user-privilege-set>'
                        f'<d:privilege><d:read/></d:privilege>'
                        f'<d:privilege><d:write/></d:privilege>'
                        f'<d:privilege><d:write-content/></d:privilege>'
                        f'<d:privilege><d:write-properties/></d:privilege>'
                        f'<d:privilege><d:bind/></d:privilege>'
                        f'<d:privilege><d:unbind/></d:privilege>'
                        f'</d:current-user-privilege-set>')

    def _cal_response_now(self):
        now = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        return (f'<d:response><d:href>{CAL}</d:href><d:propstat><d:prop>'
                f'{self._CAL_PROPS_BASIC}'
                f'<cs:getctag>{now}</cs:getctag>'
                f'</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>')

    def _collection_propfind(self, depth):
        now = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        if depth in ("1", "infinity"):
            # enumerate events for ctag/etag listing over a wide window
            st = time.strftime("%Y-%m-%dT00:00:00Z", time.gmtime(time.time() - 180 * 86400))
            en = time.strftime("%Y-%m-%dT00:00:00Z", time.gmtime(time.time() + 180 * 86400))
            try:
                evs = build_event_list(st, en)
            except Exception as e:
                elog(f"PROPFIND-depth1 find fail: {e}")
                evs = []
            elog(f"depth1 listing: {len(evs)} events over {st}..{en}")
            items = [f'<d:response><d:href>{CAL}</d:href><d:propstat><d:prop>'
                     f'{self._CAL_PROPS_BASIC}<cs:getctag>{now}</cs:getctag>'
                     f'</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>']
            for uid, it in evs:
                items.append(
                    f'<d:response><d:href>{CAL}{uid}.ics</d:href><d:propstat><d:prop>'
                    f'<d:resourcetype/>'
                    f'<d:getetag>{etag(it["changekey"])}</d:getetag>'
                    f'<d:getcontenttype>text/calendar; charset=utf-8</d:getcontenttype>'
                    f'</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>')
            return self._ms(items)
        return self._ms([f'<d:response><d:href>{CAL}</d:href><d:propstat><d:prop>'
                         f'{self._CAL_PROPS_BASIC}<cs:getctag>{now}</cs:getctag>'
                         f'</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>'])

    def do_PROPFIND(self):
        path = unquote(self.path).rstrip("/")
        if not path:
            path = "/"
        depth = self.headers.get("Depth", "0")
        self._logreq()
        if path.startswith("/.well-known/caldav"):
            self.send_response(301)
            self.send_header("Location", CAL)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        try:
            if path == "/" or path == DAV.rstrip("/") or path == "/.well-known" or path == "/dav/principals":
                self._send(207, self._root_propfind(depth))
            elif path == PRIN.rstrip("/"):
                self._send(207, self._ms([f'<d:response><d:href>{PRIN}</d:href><d:propstat><d:prop>'
                                          f'<d:resourcetype><d:principal/></d:resourcetype>'
                                          f'<c:calendar-home-set><d:href>{HOME}</d:href></c:calendar-home-set>'
                                          f'<d:current-user-principal><d:href>{PRIN}</d:href></d:current-user-principal>'
                                          f'</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>']))
            elif path == HOME.rstrip("/") or path == HOME.replace(USER_H, USER).rstrip("/"):
                self._send(207, self._ms([self._home_response(depth)]))
            elif path == CAL.rstrip("/"):
                self._send(207, self._collection_propfind(depth))
            else:
                self._send(404, b"<?xml version=\"1.0\"?><D:error xmlns:D=\"DAV:\"><D:not-found/></D:error>")
        except Exception as e:
            import traceback
            elog("PROPFIND ERR " + traceback.format_exc().replace("\n", " | "))
            self._send(500, b"<error/>")

    # ---- REPORT -------------------------------------------------------------
    def _parse_time_range(self, body):
        tr = rb"<(?:\w+:)?time-range[^>]*>"
        m = re.search(rb"<(?:[a-z]\w*:)?time-range[^>]*?start=\"([^\"]+)\"[^>]*?end=\"([^\"]+)\"", body, re.I)
        if m:
            return m.group(1).decode(), m.group(2).decode()
        m = re.search(rb"<(?:[a-z]\w*:)?time-range[^>]*?end=\"([^\"]+)\"[^>]*?start=\"([^\"]+)\"", body, re.I)
        if m:
            return m.group(1).decode(), m.group(2).decode()
        m = re.search(rb"<(?:[a-z]\w*:)?time-range[^>]*?start=\"([^\"]+)\"", body, re.I)
        s = m.group(1).decode() if m else None
        m = re.search(rb"<(?:[a-z]\w*:)?time-range[^>]*?end=\"([^\"]+)\"", body, re.I)
        e = m.group(1).decode() if m else None
        return s, e

    def _to_ewgrave(self, t):
        if not t:
            return None
        t = t.replace("Z", "")
        if re.match(r"^\d{8}T\d{6}$", t):
            return f"{t[:4]}-{t[4:6]}-{t[6:8]}T{t[9:11]}:{t[11:13]}:{t[13:15]}Z"
        return None

    def do_REPORT(self):
        path = unquote(self.path)
        body = self._logreq()
        try:
            if path.rstrip("/") != CAL.rstrip("/"):
                self._send(404, b"")
                return
            if b"calendar-query" in body:
                s, e = self._parse_time_range(body)
                st = self._to_ewgrave(s) or time.strftime("%Y-%m-%dT00:00:00Z", time.gmtime(time.time() - 180 * 86400))
                en = self._to_ewgrave(e) or time.strftime("%Y-%m-%dT00:00:00Z", time.gmtime(time.time() + 180 * 86400))
                evs = build_event_list(st, en)
                items = [f'<d:response><d:href>{CAL}{uid}.ics</d:href>'
                         f'<d:propstat><d:prop>'
                         f'<d:getetag>{etag(it["changekey"])}</d:getetag>'
                         f'<c:calendar-data>{x(to_ics(uid, it))}</c:calendar-data>'
                         f'</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>'
                         for uid, it in evs]
                self._send(207, self._ms(items))
            elif b"calendar-multiget" in body:
                hrefs = re.findall(rb"<(?:\w+:)?href>([^<]+)</(?:\w+:)?href>", body, re.I)
                items = []
                for h in hrefs:
                    hh = h.decode().split("/")[-1]
                    if hh.endswith(".ics"):
                        uid = hh[:-4]
                    else:
                        uid = hh.split("/")[-1]
                    it = _CACHE.get(uid)
                    if it:
                        items.append(f'<d:response><d:href>{CAL}{uid}.ics</d:href>'
                                     f'<d:propstat><d:prop>'
                                     f'<d:getetag>{etag(it["changekey"])}</d:getetag>'
                                     f'<c:calendar-data>{x(to_ics(uid, it))}</c:calendar-data>'
                                     f'</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>')
                    else:
                        items.append(f'<d:response><d:href>{hh}</d:href>'
                                     f'<d:status>HTTP/1.1 404 Not Found</d:status></d:response>')
                self._send(207, self._ms(items))
            else:
                self._send(400, b"<error/>")
        except Exception as ex:
            import traceback
            elog("REPORT ERR " + traceback.format_exc().replace("\n", " | "))
            self._send(500, b"<error/>")

    # ---- GET ----------------------------------------------------------------
    def _get_single(self, uid):
        it = _CACHE.get(uid)
        if not it:
            st = time.strftime("%Y-%m-%dT00:00:00Z", time.gmtime(time.time() - 180 * 86400))
            en = time.strftime("%Y-%m-%dT00:00:00Z", time.gmtime(time.time() + 180 * 86400))
            evs = build_event_list(st, en)
            it = _CACHE.get(uid)
        if not it:
            return None
        return to_ics(uid, it, full=True).encode("utf-8")

    def do_GET(self):
        path = unquote(self.path)
        self._logreq()
        if path.startswith("/.well-known/caldav"):
            self.send_response(301)
            self.send_header("Location", CAL)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        try:
            if path.rstrip("/") == CAL.rstrip("/"):
                st = time.strftime("%Y-%m-%dT00:00:00Z", time.gmtime(time.time() - 180 * 86400))
                en = time.strftime("%Y-%m-%dT00:00:00Z", time.gmtime(time.time() + 180 * 86400))
                evs = build_event_list(st, en)
                ics = ("".join(to_ics(uid, it) for uid, it in evs)).encode("utf-8")
                self._send(200, ics, "text/calendar; charset=utf-8")
            elif path.startswith(CAL) and path.endswith(".ics"):
                uid = path[len(CAL):-4]
                data = self._get_single(uid)
                if data is None:
                    self._send(404)
                else:
                    self._send(200, data, "text/calendar; charset=utf-8")
            else:
                self._send(404)
        except Exception as ex:
            import traceback
            elog("GET ERR " + traceback.format_exc().replace("\n", " | "))
            self._send(500)

    # ---- write ops: M2 ------------------------------------------------------
    def do_PUT(self):
        path = unquote(self.path)
        body = self._body()
        elog(f"PUT {path} len={len(body)} first={body[:80]!r}")
        try:
            if not path.startswith(CAL) or not path.endswith(".ics"):
                self._send(404, b"<error/>")
                return
            uid = path[len(CAL):-4]
            try:
                with open("/tmp/ewscaldav-last-put.ics", "wb") as _f:
                    _f.write(body)
            except Exception:
                pass
            ev = parse_ics(body)
            if not ev or not ev.get("start"):
                self._send(400, b"<error/>")
                return
            c = db()
            row = c.execute("SELECT itemid, changekey FROM ev WHERE uid=?", (uid,)).fetchone()
            if row:
                ok = ews_update(ev, row[0], row[1])
                if ok:
                    c.execute("UPDATE ev SET changekey=?, attendees=? WHERE uid=?",
                              (_fetch_changekey(row[0]),
                               json.dumps(event_attendees(ev)), uid))
                    c.commit()
                code = 204 if ok else 500
            else:
                res = ews_create(ev)
                if not res:
                    self._send(500, b"<error/>")
                    return
                c.execute("INSERT OR IGNORE INTO ev(uid,itemid,changekey,attendees) VALUES(?,?,?,?)",
                          (uid, res["itemid"], res["changekey"],
                           json.dumps(event_attendees(ev))))
                c.commit()
                code = 201
            c.close()
            _CACHE.pop(uid, None)
            self._send(code)
        except Exception as ex:
            import traceback
            elog("PUT ERR " + traceback.format_exc().replace("\n", " | "))
            self._send(500, b"<error/>")

    def do_DELETE(self):
        path = unquote(self.path)
        self._logreq()
        try:
            if not path.startswith(CAL) or not path.endswith(".ics"):
                self._send(404, b"<error/>")
                return
            uid = path[len(CAL):-4]
            c = db()
            row = c.execute("SELECT itemid FROM ev WHERE uid=?", (uid,)).fetchone()
            if not row:
                c.close()
                self._send(404, b"<error/>")
                return
            ok = ews_delete(row[0])
            if ok:
                c.execute("DELETE FROM ev WHERE uid=?", (uid,))
                c.commit()
            c.close()
            _CACHE.pop(uid, None)
            self._send(204 if ok else 500)
        except Exception as ex:
            import traceback
            elog("DELETE ERR " + traceback.format_exc().replace("\n", " | "))
            self._send(500, b"<error/>")

    def do_POST(self):
        self._send(501, b"<M1 read-only/>")
    def do_PROPPATCH(self):
        self._send(501, b"<M1 read-only/>")
    def do_MKCALENDAR(self):
        self._send(501, b"<M1 read-only/>")
    def do_HEAD(self):
        self.do_GET()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8081)
    ar = ap.parse_args()
    _c = b.load_conf()
    b.ARGS = argparse.Namespace(host=_c["host"], port=_c["port"], user=_c["user"],
                                password=_c["password"], proxy=_c.get("proxy"),
                                sni=_c.get("sni", _c["host"]),
                                lport=ar.port, listen=ar.listen, log=_c["log"])
    b.LOG = LOG
    srv = ThreadingHTTPServer((ar.listen, ar.port), Handler)
    elog(f"CalDAV up on {ar.listen}:{ar.port} calendar={CAL}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()

if __name__ == "__main__":
    main()