#!/usr/bin/env python3
import socket, ssl, argparse, re, sys, time, socketserver, subprocess, os, threading, base64, hashlib
from html import unescape as _htmlescape
from urllib.parse import urlsplit, parse_qs
from email import message_from_bytes, encoders
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase

CRED_FILE = os.environ.get("EWS_CRED",
                           os.path.expanduser("~/.config/ews-bridge/cred.json"))

def load_conf():
    import json
    cfg = {"host": "mail.example.com", "port": 443, "sni": "mail.example.com",
           "user": "", "password": "", "proxy": None,
           "listen": "127.0.0.1", "lport": 17080,
           "log": "/tmp/ews-bridge.log"}
    try:
        with open(CRED_FILE) as fh:
            cfg.update(json.load(fh))
    except Exception:
        pass
    return cfg

def log(msg, fh):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    fh.write(f"[{ts}] {msg}\n")
    fh.flush()

def rewrite(body):
    txt = body.decode("utf-8", "replace")
    new = re.sub(r'Id\s*=\s*["\']archive["\']', 'Id="inbox"', txt)
    new = re.sub(r"<\s*(?:\w+:)?InternetMessageId[^>]*>.+?</\s*(?:\w+:)?InternetMessageId\s*>", "", new, flags=re.S)
    return new.encode("utf-8"), new != txt

CK_CACHE = {}
CK_ERRS = (b"ErrorChangeKeyRequiredForWriteOperations",
           b"ErrorChangeKeyMismatch", b"ErrorIrresolvableConflict",
           b"ErrorChangeKeyRequiredForDeleteOperations")
WRITE_RE = re.compile(rb"<(?:\w+:)?(?:UpdateItem|DeleteItem|MoveItem|CopyItem)\b")
ITEMID_RE = re.compile(rb"<(?:\w+:)?ItemId\b[^>]*>")

def _tag_attrs(tag):
    return dict(re.findall(rb'(\w+)="([^"]*)"', tag))

def ews_get_changekeys(ids):
    if not ids:
        return {}
    items = "".join(f'<t:ItemId Id="{i}"/>' for i in ids)
    xml = ('<?xml version="1.0" encoding="utf-8"?>'
           '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"'
           ' xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types">'
           '<soap:Header><t:RequestServerVersion Version="Exchange2010_SP2"/></soap:Header>'
           '<soap:Body><GetItem xmlns="http://schemas.microsoft.com/exchange/services/2006/messages">'
           '<ItemShape><t:BaseShape>IdOnly</t:BaseShape></ItemShape>'
           f'<ItemIds>{items}</ItemIds></GetItem></soap:Body></soap:Envelope>')
    hdrs = [("Content-Type", "text/xml; charset=utf-8"),
            ("SOAPAction", '"http://schemas.microsoft.com/exchange/services/2006/messages/GetItem"')]
    try:
        code, _, out = ews_via_curl("/ews/exchange.asmx", xml.encode("utf-8"), hdrs)
    except Exception as e:
        log(f"CK-GETITEM-ERR {e}", LOG)
        return {}
    found = {}
    if code == 200:
        for m in re.finditer(rb"<(?:\w+:)?ItemId\b([^>]*)>", out):
            a = _tag_attrs(m.group(1))
            if b"Id" in a and b"ChangeKey" in a:
                found[a[b"Id"].decode("utf-8", "replace")] = a[b"ChangeKey"].decode("utf-8", "replace")
    else:
        log(f"CK-GETITEM http={code}", LOG)
    return found

def ensure_changekeys(body, force_ids=None):
    if not WRITE_RE.search(body):
        return body, False
    force_ids = force_ids or set()
    tags = list(ITEMID_RE.finditer(body))
    need = []
    for m in tags:
        a = _tag_attrs(m.group(0))
        iid = a.get(b"Id")
        if not iid:
            continue
        s = iid.decode("utf-8", "replace")
        if s in force_ids or s not in CK_CACHE:
            need.append(s)
    if need:
        got = ews_get_changekeys(need)
        CK_CACHE.update(got)
        for s in need:
            if s not in CK_CACHE:
                log(f"CK-MISS no changekey for {s[:40]}...", LOG)
    out, last, changed = [], 0, False
    for m in tags:
        a = _tag_attrs(m.group(0))
        iid = a.get(b"Id")
        if not iid:
            continue
        ck = CK_CACHE.get(iid.decode("utf-8", "replace"))
        if not ck:
            continue
        tag = m.group(0)
        if a.get(b"ChangeKey") == ck.encode("utf-8"):
            continue
        if b"ChangeKey" in a:
            newtag = re.sub(rb'ChangeKey="[^"]*"', b'ChangeKey="' + ck.encode("utf-8") + b'"', tag)
        else:
            newtag = re.sub(rb'(Id="[^"]*")', rb'\1 ChangeKey="' + ck.encode("utf-8") + b'"', tag, count=1)
        out.append(body[last:m.start()])
        out.append(newtag)
        last = m.end()
        changed = True
    if not changed:
        return body, False
    out.append(body[last:])
    log(f"CK-INJ ids={len(tags)} fetched={len(need)}", LOG)
    return b"".join(out), True

def body_item_ids(body):
    ids = set()
    for m in ITEMID_RE.finditer(body):
        a = _tag_attrs(m.group(0))
        if b"Id" in a:
            ids.add(a[b"Id"].decode("utf-8", "replace"))
    return ids

def harvest_changekeys(xml):
    for m in re.finditer(rb"<(?:\w+:)?ItemId\b([^>]*)>", xml):
        a = _tag_attrs(m.group(1))
        if b"Id" in a and b"ChangeKey" in a:
            CK_CACHE[a[b"Id"].decode("utf-8", "replace")] = a[b"ChangeKey"].decode("utf-8", "replace")

MIME_CACHE = {}
MIME_CACHE_ORDER = []
MIME_CACHE_N = 64
MIME_CACHE_MAXB = 96 * 1024 * 1024
MIME_CACHE_BYTES = 0
MIMECONTENT_RE = re.compile(
    rb"<(?:\w+:)?MimeContent\b[^>]*>(.*?)</(?:\w+:)?MimeContent\s*>", re.S)
XMOZ_RE = re.compile(rb"""x-moz-ews://[^"'<>\s]+""")
CREATEITEM_RE = re.compile(rb"<(?:\w+:)?CreateItem\b")

def _cache_put(fname, ctype, data):
    global MIME_CACHE_BYTES
    try:
        fname = os.path.basename(fname or "").strip()
        if not fname or not data:
            return False
        old = MIME_CACHE.pop(fname, None)
        if old:
            MIME_CACHE_BYTES -= len(old[1])
            if fname in MIME_CACHE_ORDER:
                MIME_CACHE_ORDER.remove(fname)
        MIME_CACHE[fname] = (ctype, data)
        MIME_CACHE_ORDER.append(fname)
        MIME_CACHE_BYTES += len(data)
        while MIME_CACHE_ORDER and (
                len(MIME_CACHE) > MIME_CACHE_N or MIME_CACHE_BYTES > MIME_CACHE_MAXB):
            k = MIME_CACHE_ORDER.pop(0)
            if k in MIME_CACHE:
                MIME_CACHE_BYTES -= len(MIME_CACHE[k][1])
                del MIME_CACHE[k]
        return True
    except Exception:
        return False

def harvest_mime(xml):
    n = f = 0
    try:
        for m in MIMECONTENT_RE.finditer(xml):
            b64 = m.group(1).strip()
            if len(b64) < 40:
                continue
            try:
                raw = base64.b64decode(b64)
            except Exception:
                continue
            if len(raw) < 32:
                continue
            try:
                msg = message_from_bytes(raw)
            except Exception:
                continue
            n += 1
            for part in msg.walk():
                if part.get_content_maintype() == "multipart":
                    continue
                fn = part.get_filename()
                if not fn:
                    continue
                try:
                    payload = part.get_payload(decode=True)
                except Exception:
                    payload = None
                if payload and _cache_put(fn, part.get_content_type(), payload):
                    f += 1
        for m in re.finditer(
                rb"<(?:\w+:)?FileAttachment>(.*?)</(?:\w+:)?FileAttachment>",
                xml, re.S):
            blk = m.group(1)
            nm = re.search(rb"<(?:\w+:)?Name>([^<]+)</(?:\w+:)?Name\s*>", blk)
            ct = re.search(rb"<(?:\w+:)?ContentType>([^<]+)</(?:\w+:)?ContentType\s*>", blk)
            co = re.search(rb"<(?:\w+:)?Content>([^<]+)</(?:\w+:)?Content\s*>", blk)
            if nm and co:
                try:
                    data = base64.b64decode(co.group(1).strip())
                    ctyp = ct.group(1).decode("utf-8", "replace") if ct else "application/octet-stream"
                    if data and _cache_put(nm.group(1).decode("utf-8", "replace"), ctyp, data):
                        f += 1
                except Exception:
                    pass
    except Exception as e:
        log(f"MIME-HARVEST-ERR {e}", LOG)
    if n or f:
        log(f"MIME-HARVEST msgs={n} files={f} cache={len(MIME_CACHE)} bytes={MIME_CACHE_BYTES}", LOG)

def _image_part(ctype, data, cid, fname):
    maintype, _, subtype = (ctype or "application/octet-stream").partition("/")
    if not subtype:
        subtype = "octet-stream"
    p = MIMEBase(maintype, subtype)
    p.set_payload(data)
    encoders.encode_base64(p)
    p.add_header("Content-ID", "<%s>" % cid)
    try:
        p.add_header("Content-Disposition", "inline", filename=fname)
    except Exception:
        p["Content-Disposition"] = "inline"
    return p

def fix_forward_images(body):
    if not CREATEITEM_RE.search(body[:3000]):
        return body, False
    m = MIMECONTENT_RE.search(body)
    if not m:
        return body, False
    try:
        mime = base64.b64decode(m.group(1).strip())
    except Exception:
        return body, False
    if b"x-moz-ews" not in mime:
        return body, False
    urls = list(dict.fromkeys(XMOZ_RE.findall(mime)))
    if not urls:
        return body, False
    repl = {}
    imgs = []
    miss = []
    for u in urls:
        try:
            uq = _htmlescape(u.decode("ascii", "replace"))
            fname = (parse_qs(urlsplit(uq).query).get("filename") or [""])[0]
        except Exception:
            fname = ""
        if not fname:
            continue
        key = os.path.basename(fname)
        if key in repl:
            continue
        ent = MIME_CACHE.get(key)
        if not ent:
            miss.append(key)
            continue
        ctype, data = ent
        cid = "img-" + hashlib.md5(key.encode("utf-8", "replace") + data).hexdigest()[:20]
        repl[key] = (u, cid)
        imgs.append((ctype, data, cid, key))
    if not repl:
        if miss:
            log(f"IMGFIX-MISS miss={miss} cache={len(MIME_CACHE)}", LOG)
        return body, False
    new_mime = mime
    placed = []
    used = set()
    for ctype, data, cid, key in imgs:
        u, _cid = repl[key]
        if u in new_mime and key not in used:
            new_mime = new_mime.replace(u, b"cid:" + cid.encode("ascii"))
            placed.append((ctype, data, cid, key))
            used.add(key)
    if not placed:
        log("IMGFIX no-url-match", LOG)
        return body, False
    try:
        msg = message_from_bytes(new_mime)
    except Exception as e:
        log(f"IMGFIX-PARSE {e}", LOG)
        return body, False
    img_parts = [_image_part(ct, data, cid, fn) for ct, data, cid, fn in placed]
    if msg.get_content_maintype() == "multipart" and msg.get_content_subtype() == "related":
        out_msg = msg
        for p in img_parts:
            out_msg.attach(p)
    else:
        rel = MIMEMultipart("related")
        keep = {"content-type", "content-transfer-encoding", "mime-version"}
        for k, v in list(msg.items()):
            if k.lower() not in keep:
                rel[k] = v
                del msg[k]
        rel.attach(msg)
        for p in img_parts:
            rel.attach(p)
        out_msg = rel
    try:
        new_bytes = out_msg.as_bytes()
    except Exception as e:
        log(f"IMGFIX-ASBYTES {e}", LOG)
        return body, False
    nb64 = base64.b64encode(new_bytes)
    body = body[:m.start(1)] + nb64 + body[m.end(1):]
    log(f"IMGFIX imgs={len(placed)} miss={len(miss)} mime {len(mime)}->{len(new_bytes)}B", LOG)
    if miss:
        log(f"IMGFIX-MISS miss={miss}", LOG)
    return body, True

def open_upstream(host, port, sni, proxy):
    if proxy:
        ph, pp = proxy.split(":")
        s = socket.create_connection((ph, int(pp)), timeout=30)
        req = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n"
        s.sendall(req.encode("ascii"))
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                raise ConnectionError("tunnel closed")
            buf += chunk
        line = buf.split(b"\r\n", 1)[0].decode("latin1")
        if "200" not in line:
            raise ConnectionError(f"tunnel refused: {line}")
    else:
        connect_host = resolve_upstream(host) or host
        s = socket.create_connection((connect_host, port), timeout=30)
    ctx = ssl.create_default_context()
    return ctx.wrap_socket(s, server_hostname=sni)

def recv_until(sock, marker):
    buf = b""
    while marker not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("connection closed")
        buf += chunk
    return buf

def read_body(sock, content_len, chunked):
    if not chunked and content_len is not None:
        data = b""
        while len(data) < content_len:
            chunk = sock.recv(min(65536, content_len - len(data)))
            if not chunk:
                raise ConnectionError("body truncated")
            data += chunk
        return data
    out = b""
    while True:
        size_line = recv_until(sock, b"\r\n").split(b"\r\n")[0]
        size = int(size_line, 16)
        if size == 0:
            while True:
                crlf = recv_until(sock, b"\r\n")
                if crlf == b"\r\n":
                    break
            break
        data = recv_until(sock, b"\r\n")
        if len(data) < size + 2:
            miss = size + 2 - len(data)
            while miss > 0:
                chunk = sock.recv(miss)
                if not chunk:
                    raise ConnectionError("chunk truncated")
                data += chunk
                miss = size + 2 - len(data)
        out += data[:size]
    return out

HOP = {"connection", "keep-alive", "proxy-connection", "transfer-encoding", "upgrade"}
SNI = None
UPSTREAM_IP = None
UPSTREAM_TS = 0

def dns_a(host):
    import struct, random
    ns = None
    try:
        with open("/etc/resolv.conf") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("nameserver"):
                    ns = line.split()[1]
                    break
    except Exception:
        pass
    if not ns:
        return None
    qid = random.randint(0, 65535)
    def qname(name):
        out = b""
        for part in name.split("."):
            out += bytes([len(part)]) + part.encode()
        return out + b"\x00"
    q = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0) + qname(host) + struct.pack(">HH", 1, 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(5)
    try:
        s.sendto(q, (ns, 53))
        data, _ = s.recvfrom(4096)
    except Exception:
        return None
    finally:
        s.close()
    if len(data) < 12:
        return None
    ancount = struct.unpack(">H", data[6:8])[0]
    if ancount == 0:
        return None
    idx = 12
    while True:
        l = data[idx]
        if l == 0:
            idx += 1
            break
        idx += l + 1
    idx += 4
    for _ in range(ancount):
        if data[idx] & 0xC0 == 0xC0:
            idx += 2
        else:
            while data[idx]:
                idx += data[idx] + 1
            idx += 1
        rtype, rclass, ttl, rdlen = struct.unpack(">HHIH", data[idx:idx + 10])
        idx += 10
        if rtype == 1 and rdlen == 4:
            ip = socket.inet_ntoa(data[idx:idx + 4])
            s.close()
            return ip
        idx += rdlen
    return None

def resolve_upstream(host):
    global UPSTREAM_IP, UPSTREAM_TS
    now = time.time()
    if re.match(r"^\d+\.\d+\.\d+\.\d+$", host):
        return host
    if UPSTREAM_IP is None or now - UPSTREAM_TS > 300:
        ip = dns_a(host)
        if ip:
            UPSTREAM_IP = ip
            UPSTREAM_TS = now
            log(f"DNS {host} -> {ip}", LOG)
    return UPSTREAM_IP

def ntlm_info(v):
    if not isinstance(v, str) or not v.startswith(("NTLM ", "Negotiate ")):
        return None
    import base64
    try:
        raw = base64.b64decode(v.split(" ", 1)[1].strip())
    except Exception:
        return None
    if len(raw) < 12 or raw[:8] != b"NTLMSSP\0":
        return None
    mtype = int.from_bytes(raw[8:12], "little")
    if mtype == 1:
        fl = int.from_bytes(raw[12:16] or b"\0\0\0\0", "little")
        dom = int.from_bytes(raw[16:18] or b"\0\0", "little")
        user = int.from_bytes(raw[24:26] or b"\0\0", "little")
        return f"T1 flags={fl:#010x} dom_len={dom} user_len={user}"
    if mtype == 2:
        ch = raw[24:32].hex() if len(raw) >= 32 else "?"
        ti = int.from_bytes(raw[40:42] or b"\0\0", "little")
        return f"T2 ch={ch} targetinfo={ti}"
    if mtype == 3:
        import posixpath
        fl = int.from_bytes(raw[60:64] or b"\0\0\0\0", "little")
        lm = int.from_bytes(raw[12:14] or b"\0\0", "little")
        nt = int.from_bytes(raw[20:22] or b"\0\0", "little")
        nt_off = int.from_bytes(raw[24:28] or b"\0\0\0\0", "little")
        user_off = int.from_bytes(raw[40:44] or b"\0\0\0\0", "little")
        user_len = int.from_bytes(raw[36:38] or b"\0\0", "little")
        ws_len = int.from_bytes(raw[44:46] or b"\0\0", "little")
        ws_off = int.from_bytes(raw[48:52] or b"\0\0\0\0", "little")
        detail = f"T3 flags={fl:#010x} LM={lm} NT={nt}"
        try:
            user = raw[user_off:user_off + user_len].decode("utf-16le", "replace")
        except Exception:
            user = "?"
        try:
            ws = raw[ws_off:ws_off + ws_len].decode("utf-16le", "replace")
        except Exception:
            ws = "?"
        detail += f" user={user!r} ws={ws!r}"
        if nt >= 32 and nt_off > 0:
            blob = raw[nt_off + 16: nt_off + nt]
            av = []
            i = 24
            while i + 4 <= len(blob):
                aid = int.from_bytes(blob[i:i + 2], "little")
                alen = int.from_bytes(blob[i + 2:i + 4], "little")
                if aid == 0:
                    break
                av.append(f"{aid}:{alen}")
                i += 4 + alen
            if av:
                detail += " av=[" + ",".join(av) + "]"
        return detail
    return f"MT{mtype} len={len(raw)}"

def ews_via_curl(path, body, req_headers):
    ip = resolve_upstream(ARGS.host)
    if not ip:
        return 502, [("Content-Type", "text/plain")], b"no upstream ip resolved"
    os.makedirs("/tmp/ewsck", exist_ok=True)
    tag = f"{os.getpid()}_{threading.get_ident()}"
    hpath = f"/tmp/ewsck/{tag}.h"
    bpath = f"/tmp/ewsck/{tag}.b"
    cmd = ["curl", "-sS", "--noproxy", "*", "--ntlm", "-u", f"{ARGS.user}:{ARGS.password}",
           "--http1.1", "--connect-timeout", "15", "--max-time", "120",
           "--resolve", f"{ARGS.host}:443:{ip}",
           "-D", hpath, "-o", bpath, "--write-out", "%{http_code}", "-X", "POST"]
    skip = HOP | {"host", "content-length", "authorization", "user-agent"}
    for k, v in req_headers:
        if k.lower() in skip:
            continue
        cmd += ["-H", f"{k}: {v}"]
    cmd += ["--data-binary", "@-", f"https://{ARGS.host}{path}"]
    try:
        p = subprocess.run(cmd, input=body, capture_output=True, timeout=150)
    except Exception as e:
        return 502, [("Content-Type", "text/plain")], f"curl-err {e}".encode()
    try:
        code = int(p.stdout.decode().strip())
    except Exception:
        code = 502
    try:
        out_body = open(bpath, "rb").read()
    except Exception:
        out_body = b""
    if p.returncode != 0:
        log(f"CURL-RC {p.returncode} code={code} got={len(out_body)}B "
            f"err={p.stderr.decode('utf-8','replace')[:300]}", LOG)
    out_headers = []
    try:
        with open(hpath) as fh:
            for ln in fh.read().split("\r\n")[1:]:
                if ":" in ln:
                    k, _, v = ln.partition(":")
                    out_headers.append((k.strip(), v.strip()))
    except Exception:
        pass
    for k, v in out_headers:
        if k.lower() == "content-length":
            try:
                if int(v) != len(out_body):
                    log(f"TRUNC content-length={v} got={len(out_body)}", LOG)
            except Exception:
                pass
    for f in (hpath, bpath):
        try:
            os.unlink(f)
        except Exception:
            pass
    return code, out_headers, out_body

def forward_to_upstream(sock, body, method, path, req_headers):
    headers = [(k, v) for k, v in req_headers if k.lower() not in HOP
               and k.lower() != "host" and k.lower() != "content-length"]
    lines = [f"{method} {path} HTTP/1.1", f"Host: {ARGS.host}:{ARGS.port}"]
    for k, v in headers:
        lines.append(f"{k}: {v}")
    lines.append(f"Content-Length: {len(body)}")
    lines.append("Connection: keep-alive")
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin1"))
    if body:
        sock.sendall(body)

def read_response(sock):
    head = recv_until(sock, b"\r\n\r\n")
    status_line = head.split(b"\r\n")[0].decode("latin1")
    headers_raw = head.split(b"\r\n\r\n")[0].split(b"\r\n")[1:]
    code = int(status_line.split(" ")[1])
    hdrs = []
    content_len = None
    chunked = False
    for line in headers_raw:
        k, _, v = line.decode("latin1").partition(":")
        v = v.strip()
        hdrs.append((k, v))
        if k.lower() == "content-length":
            content_len = int(v)
        if k.lower() == "transfer-encoding" and "chunked" in v.lower():
            chunked = True
    body = read_body(sock, content_len, chunked)
    return code, hdrs, body

class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            sock = self.request
            log(f"CONN {sock.getpeername()[0]}:{sock.getpeername()[1]}", LOG)
            upstream = None
            try:
                while True:
                    first = recv_until(sock, b"\r\n\r\n")
                    if not first:
                        break
                    headpart, _, tail = first.partition(b"\r\n\r\n")
                    method, path, _ = headpart.split(b"\r\n")[0].decode("latin1").split(" ", 2)
                    headers_raw = headpart.split(b"\r\n")[1:]
                    req_headers = []
                    content_len = 0
                    for line in headers_raw:
                        k, _, v = line.decode("latin1").partition(":")
                        v = v.strip()
                        req_headers.append((k, v))
                        if k.lower() == "content-length":
                            content_len = int(v)
                    if content_len > len(tail):
                        tail += read_body(sock, content_len - len(tail), False)
                    elif content_len < len(tail):
                        tail = tail[:content_len]
                    body = tail
                    def _authhdr():
                        for k, v in req_headers:
                            if k.lower() == "authorization":
                                return v.split(" ")[0]
                        return "-"
                    auth_val = None
                    for k, v in req_headers:
                        if k.lower() == "authorization":
                            auth_val = v
                    auth_info = ntlm_info(auth_val) if auth_val else None
                    if path.lower().endswith("exchange.asmx") and body and method in ("POST",):
                        new_body, changed = rewrite(body)
                        body = new_body
                        body, ck_inj = ensure_changekeys(body)
                        body, img_inj = fix_forward_images(body)
                        content_len = len(body)
                        req_headers = [(k, v) if k.lower() != "content-length" else ("Content-Length", str(content_len)) for k, v in req_headers]
                        log(f"<- {method} {path} body={len(body)}B rewrite={changed} ck={ck_inj} img={img_inj} auth={_authhdr()}{' ' + auth_info if auth_info else ''}", LOG)
                    else:
                        log(f"<- {method} {path} body={len(body)}B auth={_authhdr()}{' ' + auth_info if auth_info else ''}", LOG)
                    if path.lower().endswith("exchange.asmx") and method in ("POST",):
                        code, out_headers, out_body = ews_via_curl(path, body, req_headers)
                        if code < 400 and any(e in out_body for e in CK_ERRS):
                            ids = body_item_ids(body)
                            for i in ids:
                                CK_CACHE.pop(i, None)
                            body, retry_inj = ensure_changekeys(body, force_ids=ids)
                            if retry_inj:
                                content_len = len(body)
                                req_headers = [(k, v) if k.lower() != "content-length" else ("Content-Length", str(content_len)) for k, v in req_headers]
                                log(f"CK-RETRY ids={len(ids)}", LOG)
                                code, out_headers, out_body = ews_via_curl(path, body, req_headers)
                        if code < 400:
                            harvest_changekeys(out_body)
                            harvest_mime(out_body)
                        if code >= 400:
                            log(f"!! ews-curl {code} for {path} body={len(out_body)}B", LOG)
                            log("   req-body=" + body.decode("utf-8", "replace")[:2000], LOG)
                            if out_body:
                                log("   resp-body=" + out_body.decode("utf-8", "replace")[:2000], LOG)
                    else:
                        if upstream is None:
                            try:
                                upstream = open_upstream(ARGS.host, ARGS.port, SNI, ARGS.proxy)
                            except Exception as e:
                                log(f"UPSTREAM-FAIL {e}", LOG)
                                try:
                                    sock.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                                except Exception:
                                    pass
                                break
                        try:
                            forward_to_upstream(upstream, body, method, path, req_headers)
                            code, out_headers, out_body = read_response(upstream)
                        except Exception:
                            upstream.close()
                            upstream = open_upstream(ARGS.host, ARGS.port, SNI, ARGS.proxy)
                            forward_to_upstream(upstream, body, method, path, req_headers)
                            code, out_headers, out_body = read_response(upstream)
                        if code >= 400:
                            log(f"!! upstream {code} for {path} body={len(out_body)}B", LOG)
                            log("   req-body=" + body.decode("utf-8", "replace")[:2000], LOG)
                            if out_body:
                                log("   resp-body=" + out_body.decode("utf-8", "replace")[:2000], LOG)
                    resp = [f"HTTP/1.1 {code} " + {400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found", 500: "Internal Server Error", 200: "OK", 207: "Multi-Status"}.get(code, "Status") + "\r\n"]
                    for k, v in out_headers:
                        if k.lower() in HOP or k.lower() == "content-length":
                            continue
                        resp.append(f"{k}: {v}\r\n")
                    resp.append(f"Content-Length: {len(out_body)}\r\n")
                    resp.append("Connection: keep-alive\r\n\r\n")
                    sock.sendall("".join(resp).encode("latin1"))
                    if out_body:
                        sock.sendall(out_body)
                    if path.lower().endswith("exchange.asmx"):
                        op = re.search(rb"Body[^>]*>\s*<(?:[^:>]+:)?([A-Za-z]+)", body[:800])
                        op = op.group(1).decode() if op else "-"
                        mark = "OK"
                        if re.search(rb"<s:Fault>|<faultstring>", out_body[:4000]):
                            fm = re.search(rb"<faultstring[^>]*>(.*?)</faultstring>", out_body[:4000], re.S)
                            mark = "FAULT:" + (fm.group(1)[:200].decode("utf-8", "replace") if fm else "?")
                        elif b"ResponseClass=\"Error\"" in out_body[:8000]:
                            rc = re.search(rb"ResponseCode>([^<]+)<", out_body[:8000])
                            mark = "ErrorRC:" + (rc.group(1).decode() if rc else "?")
                        log(f"-> {code} op={op} {len(out_body)}B [{mark}]", LOG)
                        if mark != "OK":
                            log("   req-body=" + body.decode("utf-8", "replace")[:2500], LOG)
                            if out_body:
                                log("   resp-body=" + out_body.decode("utf-8", "replace")[:2500], LOG)
            finally:
                if upstream:
                    upstream.close()
        except Exception as e:
            import traceback
            log("ERR " + traceback.format_exc().replace("\n", " | "), LOG)

class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True

def main():
    log(f"EWS bridge up on {ARGS.listen}:{ARGS.lport} -> {ARGS.host}:{ARGS.port}", LOG)
    server = Server((ARGS.listen, ARGS.lport), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

if __name__ == "__main__":
    _c = load_conf()
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", default=_c.get("listen", "127.0.0.1"))
    ap.add_argument("--lport", type=int, default=_c.get("lport", 17080))
    ap.add_argument("--host", default=_c.get("host", "mail.example.com"))
    ap.add_argument("--port", type=int, default=_c.get("port", 443))
    ap.add_argument("--sni", default=_c.get("sni", _c.get("host", "mail.example.com")))
    ap.add_argument("--proxy", default=_c.get("proxy"))
    ap.add_argument("--user", default=_c.get("user", ""))
    ap.add_argument("--password", default=_c.get("password", ""))
    ap.add_argument("--log", default=_c.get("log", "/tmp/ews-bridge.log"))
    ARGS = ap.parse_args()
    SNI = ARGS.sni
    LOG = open(ARGS.log, "a")
    main()