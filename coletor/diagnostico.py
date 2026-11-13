"""Diagnóstico: arquivo de cadastro de balcão da B3, tabela CRI/CRA da ANBIMA, SND."""
import collections, csv, io, json, re, time, urllib.request, urllib.error, zipfile
from datetime import date, timedelta

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def req(url, corpo=None, timeout=30, tent=5):
    h = {"User-Agent": UA, "Accept": "*/*"}
    if corpo is not None:
        h["Content-Type"] = "application/json"
    err = ""
    for i in range(tent):
        try:
            r = urllib.request.urlopen(urllib.request.Request(url, data=corpo, headers=h), timeout=timeout)
            return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()[:300]
        except Exception as e:
            err = repr(e)
        time.sleep(2)
    return None, err.encode()


d = date.today() - timedelta(days=1)
while d.weekday() >= 5:
    d -= timedelta(days=1)

print("=" * 80, "\nB3 arquivo de cadastro (api/download)")
for nome in ("OTCInstrumentsConsolidatedFile", "OTCInstrumentsConsolidated", "InstrumentsConsolidatedFile"):
    st, b = req(f"https://arquivos.b3.com.br/api/download/requestname?fileName={nome}&date={d}")
    print(nome, st, b[:300])
    if st != 200:
        continue
    try:
        tok = json.loads(b)["token"]
    except Exception:
        continue
    st, f = req(f"https://arquivos.b3.com.br/api/download/?token={tok}", timeout=120)
    print("  download", st, len(f), f[:4])
    if st != 200:
        continue
    if f[:2] == b"PK":
        z = zipfile.ZipFile(io.BytesIO(f)); f = z.read(z.namelist()[0])
    txt = f.decode("latin-1")
    ls = txt.splitlines()
    print("  linhas:", len(ls))
    for l in ls[:3]:
        print("   ", l[:700])
    hdr_i = next(i for i, l in enumerate(ls[:5]) if l.count(";") > 5)
    rd = list(csv.reader(ls[hdr_i:], delimiter=";"))
    cab = rd[0]
    print("  cabeçalho:", cab)
    for ti, c in enumerate(cab):
        cnt = collections.Counter(r[ti] for r in rd[1:] if len(r) > ti)
        if 2 < len(cnt) < 80 and ("DEB" in cnt or "CRA" in cnt):
            print("  tipos em", c, cnt.most_common(30))
    n = 0
    for r in rd[1:]:
        lin = ";".join(r).upper()
        if "RAIZEN" in lin and n < 3:
            print("   raizen:", dict(zip(cab, r))); n += 1
    break

print("=" * 80, "\nANBIMA CRI/CRA (HTML)")
st, b = req("https://www.anbima.com.br/pt_br/informar/precos-e-indices/precos/taxas-de-cri-e-cra/taxas-de-cri-e-cra.htm", timeout=60, tent=2)
h = b.decode("utf-8", errors="replace")
print("status", st, len(h), "tabelas:", h.count("<table"))
i = h.find("<table")
if i >= 0:
    trecho = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "|", h[i:i + 8000]))
    print(re.sub(r"\|( ?\|)+", "|", trecho)[:2500])
for m in re.findall(r".{0,250}exportarCSV.{0,250}", h)[:3]:
    print("ctx export:", re.sub(r"\s+", " ", m))

print("=" * 80, "\nSND debentures.com.br")
for u in ("https://www.debentures.com.br/exploreosnd/consultaadados/emissoesdedebentures/caracteristicas_r.asp?tip_deb=publicas&selecao=RAIZ",
          "https://www.debentures.com.br/exploreosnd/consultaadados/emissoesdedebentures/puhistorico_r.asp"):
    st, b = req(u, timeout=30, tent=2)
    print(u, st, len(b), b[:200])
