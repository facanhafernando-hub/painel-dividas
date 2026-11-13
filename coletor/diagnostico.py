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

# procura linhas de DEB/CRA no cadastro (varre algumas páginas)
achou = collections.Counter()
for p in range(1, 60, 5):
    st, b = req(f"https://arquivos.b3.com.br/bdi/table/InstrumentRegistration/{d}/{d}/{p}/1000", b"{}")
    if st != 200:
        print("pág", p, st); continue
    t = json.loads(b)["table"]
    if not t["values"]:
        print("fim na pág", p); break
    cols = [c["name"] for c in t["columns"]]
    for v in t["values"]:
        row = dict(zip(cols, v))
        tipo = next((str(row[k]) for k in row if "nstrument" in k or "Tp" in k), "?")
        achou[tipo] += 1
        if any(x in json.dumps(v, ensure_ascii=False) for x in ("RAIZEN", "AMERICANAS")) and achou["_amostra"] < 6:
            achou["_amostra"] += 1
            print("  amostra:", row)
print("tipos vistos:", achou.most_common(30))

print("=" * 80, "\nANBIMA CRI/CRA")
for u in ("https://www.anbima.com.br/pt_br/informar/precos-e-indices/precos/taxas-de-cri-e-cra/taxas-de-cri-e-cra.htm",
          "https://www.anbima.com.br/pt_br/informar/precos-e-indices/precos/taxas-de-cri-e-cra.htm"):
    st, b = req(u, timeout=30, tent=2)
    print(u, st, len(b))
    if st == 200:
        links = sorted(set(re.findall(rb'(?:href|src|data-url)="([^"]+)"', b)))
        for l in links:
            if re.search(rb"(xls|csv|txt|cri|cra|download|arqs|\.asp)", l, re.I):
                print("   ", l.decode(errors="replace"))
        for m in re.findall(rb".{0,120}(?:CRI_CRA|cri_cra|\.xlsx).{0,120}", b)[:10]:
            print("   ctx:", m.decode(errors="replace"))
