import socket
def enc(t,p): 
    p=p.encode() if isinstance(p,str) else p; l=len(p)
    if l<0x80: return bytes([t,l])+p
    lb=l.to_bytes((l.bit_length()+7)//8,'big'); return bytes([t,0x80|len(lb)])+lb+p
def seq(*x): return enc(0x30,b"".join(x))
def msg(mid, op): return enc(0x30, enc(0x02,(mid).to_bytes(2,'big'))+op)

# Bind
br = enc(0x02,b"\x03") + enc(0x04,b"") + enc(0x80,b"")
# Search filter (cn=*fu*)
sub = enc(0x04,b"cn") + enc(0x30, enc(0x80,b"li"))
filt = enc(0x84, sub)
inner = enc(0x04,b"dc=example,dc=com") + enc(0x0a,b"\x02") + enc(0x0a,b"\x00") \
      + enc(0x02,b"\x00\x00") + enc(0x02,b"\x00\x00") + enc(0x01,b"\x00") + filt \
      + enc(0x30, b"".join(enc(0x04,a) for a in
          ["cn","sn","givenName","mail","title","department","company","mobile","objectClass"]))
s=socket.create_connection(("127.0.0.1",17089), timeout=30)
s.sendall(msg(1, enc(0x60,br)))
r=s.recv(65536); print("bind:", "OK" if b"\x61" in r[:20] else r[:40])
s.sendall(msg(2, enc(0x63,inner)))
buf=b""; entries=0; attrs_seen=set()
while True:
    d=s.recv(65536)
    if not d: break
    buf+=d
    while True:
        if len(buf)<2: break
        h=2; l=buf[1]
        if l&0x80:
            n=l&0x7f
            if len(buf)<2+n: break
            l=int.from_bytes(buf[2:2+n],'big'); h=2+n
        if len(buf)<h+l: break
        pkt=buf[:h+l]; buf=buf[h+l:]
        if pkt[h:h+2]==b"\x02\x01": op=pkt[h+3]
        else: op=pkt[h+2]
        if op==0x61: print("bindResponse")
        elif op==0x64:
            entries+=1
            import re
            m=re.findall(rb"\x04(.)(.)",pkt)  # unaesthetic; skip detail
        elif op==0x65:
            print("done; entries=%d" % entries); s.close(); 
            import sys; sys.exit(0)
