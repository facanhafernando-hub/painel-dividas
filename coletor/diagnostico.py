"""Diagnóstico: cadastro de instrumentos da B3 e fonte de CRI/CRA da ANBIMA."""
import collections, json, re, time, urllib.request, urllib.error
from datetime import date, timedelta

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def req(url, corpo=None, timeout=15, tent=8):
    h = {"User-Agent": UA, "Accept": "*/*"}
    if corpo is not None:
        h["Content-Type"] = "application/json"
    for i in range(tent):
        try:
            r = urllib.request.urlopen(urllib.request.Request(url, data=corpo, headers=h), timeout=timeout)
            return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, b""
        except Exception as e:
            err = repr(e)
        time.sleep(1)
    return None, err.encode()


d = date.today() - timedelta(days=1)
while d.weekday() >= 5:
    d -= timedelta(days=1)

print("=" * 80, "\nB3 InstrumentRegistration", d)
st, b = req(f"https://arquivos.b3.com.br/bdi/table/InstrumentRegistration/{d}/{d}/1/20", b"{}")
print("status", st)
try:
    j = json.loads(b)
    t = j["table"]
    print("colunas:", [(c["name"], c.get("friendlyNamePt")) for c in t["columns"]])
    for v in t["values"][:6]:
        print("  ", v)
    print("outras chaves:", {k: v for k, v in j.items() if k != "table"})
except Exception as e:
    print("erro", e, b[:300])

# varre o cadastro inteiro em paralelo, contando tipos e guardando amostras
from concurrent.futures import ThreadPoolExecutor
t0 = time.time()
def pag(p):
    st, b = req(f"https://arquivos.b3.com.br/bdi/table/InstrumentRegistration/{d}/{d}/{p}/1000", b"{}")
    return json.loads(b)["table"]["values"] if st == 200 else None
tot = []
p = 1
with ThreadPoolExecutor(8) as ex:
    while p < 400:
        res = list(ex.map(pag, range(p, p + 8)))
        falhou = [p + i for i, r in enumerate(res) if r is None]
        if falhou: print("falhas", falhou)
        fim = False
        for r in res:
            if r is None: continue
            if not r: fim = True; break
            tot.extend(r)
        if fim: break
        p += 8
print(f"cadastro: {len(tot)} linhas em {time.time()-t0:.0f}s")
print("tipos:", collections.Counter(r[4] for r in tot).most_common(40))
for alvo in ("RAIZEN", "AMERICANAS"):
    rs = [r for r in tot if alvo in str(r[3]).upper() and r[4] in ("DEB", "CRA", "CRI")][:5]
    for r in rs: print("  ", alvo, r)
print("indexadores DEB:", collections.Counter(r[8] for r in tot if r[4] == "DEB").most_common(10))

print("=" * 80, "\nANBIMA CRI/CRA")
st, b = req("https://www.anbima.com.br/pt_br/anbima/TaxasCriCraExport/exportarCSV?filtroTermo=", timeout=60, tent=3)
print("CSV status", st, len(b))
txt = b.decode("utf-8-sig", errors="replace") if b"\xc3" in b[:5000] else b.decode("latin-1", errors="replace")
for l in txt.splitlines()[:12]:
    print("   ", l[:500])
print("   linhas:", len(txt.splitlines()))
for u in ():
    st, b = req(u, timeout=30, tent=2)
    print(u, st, len(b))
    if st == 200:
        links = sorted(set(re.findall(rb'(?:href|src|data-url)="([^"]+)"', b)))
        for l in links:
            if re.search(rb"(xls|csv|txt|cri|cra|download|arqs|\.asp)", l, re.I):
                print("   ", l.decode(errors="replace"))
        for m in re.findall(rb".{0,120}(?:CRI_CRA|cri_cra|\.xlsx).{0,120}", b)[:10]:
            print("   ctx:", m.decode(errors="replace"))
