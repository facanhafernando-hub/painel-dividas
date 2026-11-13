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

print("=" * 80, "\nANBIMA CRI/CRA CSV")
for fd in (d.strftime("%d/%m/%Y"), d.strftime("%Y-%m-%d"), d.strftime("%d%m%Y")):
    st, b = req(f"https://www.anbima.com.br/pt_br/anbima/TaxasCriCraExport/exportarCSV?filtroTermo=&filtroData={urllib.request.quote(fd)}")
    print(fd, st, len(b))
    if st == 200 and len(b) > 200:
        t = b.decode("utf-8-sig", errors="replace")
        if "Ã" in t[:3000] or "�" in t[:3000]:
            t = b.decode("latin-1")
        ls = t.splitlines()
        print("  linhas:", len(ls))
        for l in ls[:6] + ls[len(ls)//2:len(ls)//2+2]:
            print("   ", l[:600])
        break

print("=" * 80, "\nSND características")
st, b = req("https://www.debentures.com.br/exploreosnd/consultaadados/emissoesdedebentures/caracteristicas_r.asp?tip_deb=publicas&selecao=RAIZ", timeout=90)
t = b.decode("latin-1")
print("status", st, len(t))
rows = re.findall(r"<tr[^>]*>(.*?)</tr>", t, re.S | re.I)
print("linhas <tr>:", len(rows))
def cells(r):
    return [H.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", c))).strip() for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, re.S | re.I)]
for r in rows[:6]:
    print("   ", cells(r)[:40])
ncol = collections.Counter(len(cells(r)) for r in rows)
print("colunas por linha:", ncol.most_common(5))
for r in rows:
    c = cells(r)
    if any("RAIZ" in x for x in c[:3]):
        print("   raizen:", c[:60]); break
for m in re.findall(r'href="([^"]+\.(?:xls|csv|txt|asp)[^"]*)"', t)[:15]:
    print("   link:", m)

print("=" * 80, "\nSND PU histórico (curva)")
for u in ("https://www.debentures.com.br/exploreosnd/consultaadados/emissoesdedebentures/puhistorico_r.asp?op_exc=False&ativo=RAIZ15&dt_ini=&dt_fim=&Submit.x=1",
          "https://www.debentures.com.br/exploreosnd/consultaadados/emissoesdedebentures/puhistorico_e.asp?op_exc=False&ativo=RAIZ15"):
    st, b = req(u, timeout=90, tent=2)
    t = b.decode("latin-1")
    print(u, st, len(t))
    rr = re.findall(r"<tr[^>]*>(.*?)</tr>", t, re.S | re.I)
    for r in rr[:8]:
        print("   ", cells(r)[:12])
