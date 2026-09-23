import importlib.util, argparse, socketserver, threading, re, os, time
import xml.sax.saxutils as X

# ---- 复用 ews-bridge 的 EWS 通道 -------------------------------
def _load_bridge():
    _path = os.environ.get("EWS_BRIDGE_PATH") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "ews_bridge.py")
    spec = importlib.util.spec_from_file_location("ewsb", _path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    cfg = mod.load_conf()
    mod.ARGS = argparse.Namespace(host=cfg["host"], port=cfg["port"],
                                  user=cfg["user"], password=cfg["password"],
                                  proxy=cfg.get("proxy"), sni=cfg.get("sni", cfg["host"]),
                                  lport=cfg.get("lport", 8080),
                                  listen=cfg.get("listen", "127.0.0.1"), log=cfg["log"])
    mod.LOG = open(cfg["log"], "a")
    return mod

b = _load_bridge()
LOG = b.load_conf().get("log", "/tmp/ews-bridge.log")
BASE = "dc=example,dc=com"
KNOWN_ATTR = ("cn", "sn", "givenname", "displayname", "mail", "title",
              "department", "company", "mobile", "telephonenumber", "l",
              "objectclass", "name")
MAX_RESULTS = 50

def elog(msg):
    try:
        with open(LOG, "a") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] LDGAL {msg}\n")
    except Exception:
        pass

# ---- BER 编解码（只读目录所需最小子集） -------------------------
def enc(tag, payload):
    if isinstance(payload, str):
        payload = payload.encode()
    l = len(payload)
    if l < 0x80:
        return bytes([tag, l]) + payload
    lx = l.to_bytes((l.bit_length() + 7) // 8, "big")
    return bytes([tag, 0x80 | len(lx)]) + lx + payload

def seq0(*parts):
    return enc(0x30, b"".join(parts))

def read_tlv(data, off):
    tag = data[off]
    off += 1
    l = data[off]
    off += 1
    if l & 0x80:
        n = l & 0x7f
        l = int.from_bytes(data[off:off + n], "big")
        off += n
    return tag, data[off:off + l], off + l

def walk_elems(v, offset=0):
    res = []
    while offset < len(v):
        tag, val, newoff = read_tlv(v, offset)
        res.append((tag, val))
        offset = newoff
    return res

# ---- EWS：ResolveNames GAL 搜索（带简单缓存） -------------------
_rescache = {}
_lock = threading.Lock()
_CACHE_TTL = 90

def _gal_search(text):
    text = text.strip()
    if not text:
        return []
    with _lock:
        hit = _rescache.get(text)
        if hit and time.time() - hit[0] < _CACHE_TTL:
            return hit[1]
    body = (f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"
 xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types"
 xmlns:m="http://schemas.microsoft.com/exchange/services/2006/messages">
<soap:Body><m:ResolveNames ReturnFullContactData="true">
<m:UnresolvedEntry>{X.escape(text)}</m:UnresolvedEntry>
</m:ResolveNames></soap:Body></soap:Envelope>""").encode()
    hdr = [("Content-Type", "text/xml; charset=utf-8"),
           ("SOAPAction", "http://schemas.microsoft.com/exchange/services/2006/messages/ResolveNames")]
    people = []
    for attempt in range(3):
        try:
            code, _oh, out = b.ews_via_curl("/ews/exchange.asmx", body, hdr)
        except Exception as e:
            elog(f"search {text!r} exc {e} (retry {attempt + 1})")
            time.sleep(0.6)
            continue
        if code != 200:
            elog(f"search {text!r} EWS code={code} (retry {attempt + 1})")
            time.sleep(0.6)
            continue
        txt = out.decode("utf-8", "replace")
        for r in re.findall(r"<t:Resolution>.*?</t:Resolution>", txt, re.S):
            def x(tag):
                m = re.search(rf"<t:{tag}[^>]*>(.*?)</t:{tag}>", r, re.S)
                return m.group(1).strip() if m else ""
            def xentry(key):
                m = re.search(rf'<t:Entry Key="{key}">(?:<t:City>([^<]*)</t:City>)?(.*?)</t:Entry>', r, re.S)
                return (m.group(1) or m.group(2)).strip() if m else ""
            p = dict(
                name=x("Name"), mail=x("EmailAddress"),
                display=x("DisplayName") or x("Name"),
                given=x("GivenName"), sn=x("Surname"),
                title=x("JobTitle"), dept=x("Department"),
                company=x("CompanyName"),
                mobile=xentry("MobilePhone"), city=xentry("Business"),
            )
            if p["name"] or p["mail"]:
                people.append(p)
        break
    if people:
        with _lock:
            if len(_rescache) > 512:
                _rescache.clear()
            _rescache[text] = (time.time(), people)
    return people

def _attr_value(p, attr):
    a = attr.lower()
    if a in ("cn", "name", "displayname"):
        return [p["display"]]
    if a == "sn":
        return [p["sn"]] if p["sn"] else []
    if a == "givenname":
        return [p["given"]] if p["given"] else []
    if a == "mail":
        return [p["mail"]] if p["mail"] else []
    if a == "title":
        return [p["title"]] if p["title"] else []
    if a in ("department", "departmentname", "ou"):
        return [p["dept"]] if p["dept"] else []
    if a in ("company", "o"):
        return [p["company"]] if p["company"] else []
    if a in ("mobile", "telephonenumber", "telephonemobile"):
        return [p["mobile"]] if p["mobile"] else []
    if a == "l":
        return [p["city"]] if p["city"] else []
    if a == "objectclass":
        return ["top", "person", "organizationalperson", "inetorgperson"]
    return []

def extract_term(filt):
    terms = []
    present = []

    def av(v):
        attr = None
        val = ""

        def dive(items):
            nonlocal attr, val
            for tg, tv in items:
                if tg == 0x30:
                    dive(walk_elems(tv))
                elif tg == 0x04:
                    if attr is None:
                        attr = tv.decode(errors="ignore")
                    else:
                        val += tv.decode(errors="ignore")
                elif tg in (0x80, 0x81, 0x82):
                    val += tv.decode(errors="ignore")

        dive(walk_elems(v))
        return (attr or ""), val

    def walk(tag, content):
        if tag in (0xa0, 0xa1):  # and / or
            for s in walk_elems(content):
                walk(s[0], s[1])
        elif tag == 0xa2:  # not
            s, _ = read_tlv(content, 0)
            walk(s[0], s[1])
        elif tag in (0xa3, 0xa4, 0xa5, 0xa6, 0xa8):  # eq/substr/ge/le/approx
            t = av(content)
            if t and t[1]:
                terms.append(t[1])
        elif tag == 0x87:  # present
            present.append(content.decode(errors="ignore"))

    walk(filt[0], filt[1])
    terms = [t.strip() for t in terms if t and t.strip()]
    if not terms:
        return ""
    terms.sort(key=len, reverse=True)
    return terms[0]

# ---- LDAP 会话 ---------------------------------------------
class LDAPHandler(socketserver.BaseRequestHandler):
    def handle(self):
        sock = self.request
        sock.settimeout(90)
        buf = b""
        try:
            while True:
                data = sock.recv(65536)
                if not data:
                    elog("conn: peer closed")
                    return
                elog(f"recv {len(data)}B head={data[:10].hex()}")
                buf += data
                while True:
                    pkt, used = self._extract(buf)
                    if pkt is None:
                        elog(f"partial: buf={len(buf)}B {buf[:10].hex()}")
                        break
                    buf = buf[used:]
                    if not self._dispatch(pkt, sock):
                        elog("conn: close by dispatch (unbind)")
                        return
                if not buf:
                    elog("conn: drain empty, waiting next recv")
        except Exception as e:
            elog(f"conn exc {e}")

    def _extract(self, buf):
        if len(buf) < 2:
            return None, 0
        l = buf[1]
        hdr = 2
        if l & 0x80:
            n = l & 0x7f
            if len(buf) < 2 + n:
                return None, 0
            l = int.from_bytes(buf[2:2 + n], "big")
            hdr = 2 + n
        total = hdr + l
        if len(buf) < total:
            return None, 0
        return buf[:total], total

    def _send(self, sock, mid, op):
        midb = enc(0x02, mid.to_bytes(1, "big")) if mid < 128 else \
               enc(0x02, mid.to_bytes(2, "big"))
        sock.sendall(enc(0x30, midb + op))

    def _dispatch(self, pkt, sock):
        tag, val, _ = read_tlv(pkt, 0)
        if tag != 0x30:
            return False
        elems = walk_elems(val)
        if not elems:
            return False
        mid = int.from_bytes(elems[0][1], "big")
        if len(elems) < 2:
            return True
        pt, pv = elems[1][0], elems[1][1]
        if pt == 0x60:  # BindRequest
            ver = 0
            if pv and pv[0] == 0x02 and len(pv) >= 3:
                ver = pv[2]
            self._send(sock, mid, enc(0x61, enc(0x0a, b"\x00\x00") +
                                                  enc(0x04, b"") + enc(0x04, b"")))
            elog(f"bind LDAPv{ver}")
            return True
        if pt == 0x63:  # SearchRequest
            self._search(sock, mid, pv)
            return True
        if pt == 0x42:  # UnbindRequest
            elog("unbind")
            return False
        if pt == 0x77:  # ExtendedRequest（StartTLS 等）-> protocolError
            self._send(sock, mid, enc(0x78, enc(0x0a, b"\x00\x02") +
                                                  enc(0x04, b"") + enc(0x04, b"")))
        elif pt in (0x75, 0x76):  # Abandon / Cancel
            pass
        else:
            elog(f"unhandled LDAP op 0x{pt:02x}")
        return True

    def _search(self, sock, mid, env):
        try:
            elems = walk_elems(env)
            if not elems:
                self._done(sock, mid, 0)
                return
            attrs = []
            filt = None
            for tg, tv in walk_elems(env):
                if tg == 0x04:          # baseObject
                    base = tv.decode(errors="ignore")
                elif tg == 0x30:        # attributes (AttributeDescriptionList)
                    attrs = [v.decode(errors="ignore") for _, v in walk_elems(tv)]
                elif tg in (0x87, 0xa0, 0xa1, 0xa2, 0xa3, 0xa4, 0xa5, 0xa6, 0xa8, 0xa9):
                    filt = (tg, tv)
            term = extract_term(filt) if filt is not None else ""
            elog(f"search base={base!r} term={term!r} attrs={attrs[:4]}")
            people = _gal_search(term) if term else []
            for p in people[:MAX_RESULTS]:
                cn = p["display"].replace("\\", "\\\\").replace(",", "\\,").replace('"', '\\"')
                self._send_entry(sock, mid, f"cn={cn},{BASE}", p, attrs)
            self._done(sock, mid, 0)
        except Exception as e:
            import traceback
            elog("search err " + traceback.format_exc().replace("\n", " | ")[:500])
            self._done(sock, mid, 2)

    def _send_entry(self, sock, mid, dn, p, attrs):
        want = [a.lower() for a in attrs] if attrs and attrs != ["1.1"] else None
        if want is None:
            want = list(KNOWN_ATTR)
        aelems = []
        for a in want:
            if a == "1.1":
                continue
            vs = _attr_value(p, a)
            if vs:
                aelems.append(seq0(enc(0x04, a),
                                   enc(0x31, b"".join(enc(0x04, v) for v in vs))))
        entry = enc(0x04, dn) + enc(0x30, b"".join(aelems))
        self._send(sock, mid, enc(0x64, entry))

    def _done(self, sock, mid, code):
        self._send(sock, mid, enc(0x65, enc(0x0a, code.to_bytes(2, "big")) +
                                  enc(0x04, b"") + enc(0x04, b"")))

if __name__ == "__main__":
    import sys
    _c = b.load_conf()
    BASE = os.environ.get("LDAP_BASE_DN", _c.get("ldap_base", "dc=example,dc=com"))
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(_c.get("ldap_port", 1389))
    elog(f"LDAP GAL server start on 127.0.0.1:{port}")
    server = socketserver.ThreadingTCPServer(("127.0.0.1", port), LDAPHandler)
    server.allow_reuse_address = True
    server.daemon_threads = True
    try:
        server.serve_forever()
    finally:
        server.server_close()