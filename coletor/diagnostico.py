"""Diagnóstico das fontes: roda no GitHub e imprime o que cada endpoint devolve."""
import collections, json, time, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def req(url, corpo=None, timeout=150):
    h = {"User-Agent": UA, "Accept": "*/*"}
    if corpo is not None:
        h["Content-Type"] = "application/json"
    t = time.time()
    try:
        r = urllib.request.urlopen(urllib.request.Request(url, data=corpo, headers=h), timeout=timeout)
        return r.status, r.read(), time.time() - t
    except urllib.error.HTTPError as e:
        return e.code, b"", time.time() - t
    except Exception as e:
        return None, repr(e).encode(), time.time() - t


def uteis(n):
    d, out = date.today(), []
    while len(out) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            out.append(d)
    return out


ref = uteis(1)[0]
print("=" * 80, f"\nB3 ConsolidatedRecords {ref}: todas as páginas em paralelo")


def pagina(p, tam=500):
    for tent in range(3):
        st, b, dt = req(f"https://arquivos.b3.com.br/bdi/table/ConsolidatedRecords/{ref}/{ref}/{p}/{tam}", b"{}")
        if st == 200:
            v = json.loads(b)["table"]["values"]
            print(f"  pág {p}: {len(v)} linhas em {dt:.1f}s", flush=True)
            return v
        print(f"  pág {p}: status {st} em {dt:.1f}s (tent {tent+1})", flush=True)
    return None


t0 = time.time()
with ThreadPoolExecutor(6) as ex:
    res = list(ex.map(pagina, range(1, 41)))
linhas = [r for v in res if v for r in v]
print(f"total {len(linhas)} linhas em {time.time()-t0:.0f}s; páginas falhas: {[i+1 for i,v in enumerate(res) if v is None]}")
tipos = collections.Counter(r[3] for r in linhas)
vol = collections.Counter()
for r in linhas:
    vol[r[3]] += r[14] or 0
print("por tipo (linhas, volume R$ mi):")
for t, n in tipos.most_common():
    print(f"  {t:8s} {n:6d}  {vol[t]/1e6:12,.1f}")
print("BusinessClass:", collections.Counter(r[15] for r in linhas).most_common(5))
for t in ("DEB", "CRI", "CRA"):
    amostra = [r for r in linhas if r[3] == t][:2]
    for a in amostra:
        print("  amostra", a)

print("=" * 80, "\nANBIMA: estrutura do arquivo e histórico")
st, b, dt = req(f"https://www.anbima.com.br/informacoes/merc-sec-debentures/arqs/db{ref:%y%m%d}.txt")
txt = b.decode("latin-1")
ls = txt.splitlines()
print("linhas:", len(ls))
sem = [l for l in ls if "@" not in l and l.strip()]
print("linhas sem @:", sem[:30])
com = [l for l in ls if "@" in l]
print("linhas com @:", len(com))
for l in com[:4] + com[len(com)//2:len(com)//2+2] + com[-3:]:
    print("   ", l[:300])
for n in (6, 10, 15, 22, 40, 120, 400):
    d = uteis(n)[-1]
    st, b, dt = req(f"https://www.anbima.com.br/informacoes/merc-sec-debentures/arqs/db{d:%y%m%d}.txt")
    print(f"  {d} (D-{n}) -> {st} {len(b)} bytes")
