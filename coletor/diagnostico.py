"""Diagnóstico: ETTJ ANBIMA (curvas PRE, IPCA e inflação implícita)."""
import urllib.request, urllib.error, urllib.parse
from datetime import date, timedelta

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def req(url, corpo=None, timeout=60):
    h = {"User-Agent": UA, "Accept": "*/*"}
    if corpo is not None:
        h["Content-Type"] = "application/x-www-form-urlencoded"
    try:
        r = urllib.request.urlopen(urllib.request.Request(url, data=corpo, headers=h), timeout=timeout)
        return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:300]
    except Exception as e:
        return None, repr(e).encode()


hoje = date.today()
datas = []
d = hoje
while len(datas) < 3:
    d -= timedelta(days=1)
    if d.weekday() < 5:
        datas.append(d)
for d in datas + [hoje - timedelta(days=45), hoje - timedelta(days=200)]:
    corpo = urllib.parse.urlencode({"Idioma": "PT", "Dt_Ref": d.strftime("%d/%m/%Y"), "saida": "csv"}).encode()
    st, b = req("https://www.anbima.com.br/informacoes/est-termo/CZ-down.asp", corpo)
    t = b.decode("latin-1", errors="replace")
    ls = t.replace("\r", "").split("\n")
    print("=" * 70, f"\n{d} status={st} bytes={len(b)} linhas={len(ls)}")
    if d == datas[0]:
        for l in ls[:60]:
            print("   ", l[:200])
    else:
        print("   ", " | ".join(ls[:3])[:300])
