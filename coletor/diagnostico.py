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
for u in (base + f"puhistorico_r.asp?op_exc=False&ativo=RAIZ15&dt_ini={ds}&dt_fim={ds}&Submit.x=1",
          base + f"puhistorico_r.asp?op_exc=False&ativo=&dt_ini={ds}&dt_fim={ds}&Submit.x=1",
          base + f"puhistorico_e.asp?op_exc=False&ativo=&dt_ini={ds}&dt_fim={ds}",
          base + f"puhistorico_e.asp?op_exc=False&ativo=RAIZ15&dt_ini={ds}&dt_fim={ds}",
          base + "caracteristicas_d.asp?tip_deb=publicas&selecao=RAIZ15"):
    st, b = req(u, timeout=90, tent=2)
    t = b.decode("latin-1")
    print(u, st, len(t))
    rr = re.findall(r"<tr[^>]*>(.*?)</tr>", t, re.S | re.I)
    print("   trs:", len(rr))
    if "_e.asp" in u:
        print("   raw:", t[:800].replace("\r", ""))
    for r in [x for x in rr if len([c for c in cells(x) if c]) >= 2][:14]:
        print("   ", [c for c in cells(r) if c][:12])
