import importlib.util, sys, argparse, re, os
spec = importlib.util.spec_from_file_location(
    "ewsb", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ews_bridge.py"))
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)

cfg = b.load_conf()
b.ARGS = argparse.Namespace(host=cfg["host"], port=cfg["port"], user=cfg["user"],
                            password=cfg["password"], proxy=cfg.get("proxy"), sni=cfg.get("sni", cfg["host"]),
                            lport=cfg.get("lport", 17080), listen=cfg.get("listen", "127.0.0.1"), log=cfg["log"])
b.LOG = open(cfg["log"], "a")

def call(action, body, tries=3):
    hdr = [("Content-Type", "text/xml; charset=utf-8"),
           ("SOAPAction", f"http://schemas.microsoft.com/exchange/services/2006/messages/{action}")]
    for i in range(tries):
        code, outh, out = b.ews_via_curl("/ews/exchange.asmx", body, hdr)
        if code == 200 and b"Fault" not in out[:600]:
            return code, out
        print(f"...retry {i+1} code={code}")
    return code, out

def dump(tag, txt, maxlen=3000):
    print(f"\n===== {tag} (len={len(txt)}) =====")
    print(txt[:maxlen])

# ---- 1) 个人联系人文件夹：FindItem AllProperties ----
soap = b"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"
 xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types"
 xmlns:m="http://schemas.microsoft.com/exchange/services/2006/messages">
<soap:Body>
<m:FindItem Traversal="Shallow"><m:ItemShape><t:BaseShape>AllProperties</t:BaseShape></m:ItemShape>
<m:ParentFolderIds><t:DistinguishedFolderId Id="contacts"/></m:ParentFolderIds></m:FindItem></soap:Body></soap:Envelope>"""
code, out = call("FindItem", soap)
txt = out.decode("utf-8", "replace")
print("CODE", code, len(txt))
if code < 400:
    items = re.findall(r'<t:Contact[ >].*?</t:Contact>', txt, re.S)
    print("contact items:", len(items))
    total = re.search(r'<t:TotalItemsInView>(\d+)</t:TotalItemsInView>', txt)
    print("TotalItemsInView:", total.group(1) if total else "?")
    if items:
        one = items[0]
        tags = sorted(set(re.findall(r'<t:([a-zA-Z]+)[ >]', one)))
        print("字段标签:", " ".join(tags))
        dump("第一条 Contact XML", one, 3500)
    else:
        dump("响应片段", txt, 2000)

# ---- 2) GAL/邮箱搜索：ResolveNames ----
for q in ["fu", "user", "zhang"]:
    soap = f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"
 xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types"
 xmlns:m="http://schemas.microsoft.com/exchange/services/2006/messages">
<soap:Body>
<m:ResolveNames ReturnFullContactData="true" SearchScope="AllContacts">
<m:UnresolvedEntry>{q}</m:UnresolvedEntry>
</m:ResolveNames></soap:Body></soap:Envelope>""".encode()
    code, out = call("ResolveNames", soap)
    txt = out.decode("utf-8", "replace")
    print(f"\n>>> ResolveNames({q}) CODE {code} len {len(txt)}")
    if code < 400:
        n = txt.count("<t:ResolutionSet")
        rs = re.search(r'<t:ResolutionSet[^>]*>.*?</t:ResolutionSet>', txt, re.S)
        dump(f"ResolveNames({q}) 响应", rs.group(0) if rs else txt[:1500], 3000)
    else:
        dump("错误", txt, 600)