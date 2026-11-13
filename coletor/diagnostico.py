"""Diagnóstico: CSV CRI/CRA ANBIMA (com data) e características de debêntures no SND."""
import collections, re, time, urllib.request, urllib.error, html as H
from datetime import date, timedelta

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def req(url, timeout=60, tent=3):
    err = ""
    for i in range(tent):
        try:
            r = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=timeout)
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

print("=" * 80, "\nSND PU histórico (curva)")
def cells(r):
    return [H.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", c))).strip() for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, re.S | re.I)]
ds = urllib.request.quote(d.strftime("%d/%m/%Y"))
base = "https://www.debentures.com.br/exploreosnd/consultaadados/emissoesdedebentures/"
for u in (base + "caracteristicas_e.asp?tip_deb=publicas&selecao=",
          base + "caracteristicas_e.asp?tip_deb=publicas",
          base + "caracteristicas_e.asp?tip_deb=publicas&selecao=RAIZ"):
    st, b = req(u, timeout=90, tent=2)
    t = b.decode("latin-1")
    print(u, st, len(t))
    rr = re.findall(r"<tr[^>]*>(.*?)</tr>", t, re.S | re.I)
    print("   trs:", len(rr))
    if "_e.asp" in u:
        ls = t.replace("\r", "").split("\n")
        print("   linhas:", len(ls))
        for l in ls[:4]: print("   ", l[:3000])
        for l in ls:
            if "RAIZ" in l[:20]: print("   raizen:", l[:3000]); break
    for r in [x for x in rr if len([c for c in cells(x) if c]) >= 2][:14]:
        print("   ", [c for c in cells(r) if c][:12])
