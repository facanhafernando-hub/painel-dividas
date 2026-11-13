"""Diagnóstico: agenda de eventos (cronograma) e documentos (escrituras) de debêntures."""
import re, urllib.request, urllib.error, urllib.parse, html as H
from datetime import date, timedelta

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def req(url, timeout=60, hdr=None):
    h = {"User-Agent": UA, "Accept": "*/*"}
    h.update(hdr or {})
    try:
        r = urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout)
        return r.status, r.read(), r.headers.get("Content-Type")
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:300], None
    except Exception as e:
        return None, repr(e).encode(), None


def mostra(nome, url, n=15, tsv=False):
    st, b, ct = req(url)
    t = b.decode("cp1252", errors="replace")
    print("=" * 70, f"\n{nome}\n{url}\nstatus={st} bytes={len(b)} ctype={ct}")
    if tsv:
        for l in t.replace("\r", "").split("\n")[:n]:
            print("   ", l[:400])
    else:
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", t, re.S | re.I)
        cel = lambda r: [H.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", c))).strip() for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, re.S | re.I)]
        rr = [c for c in (cel(r) for r in rows) if len([x for x in c if x]) >= 3]
        print("   linhas úteis:", len(rr))
        for c in rr[:n]:
            print("   ", [x for x in c if x][:12])
        for m in sorted(set(re.findall(r'(?:href|action)="([^"]*(?:agenda|evento|_e\.asp|_r\.asp|_f\.asp)[^"]*)"', t, re.I)))[:20]:
            print("    link:", m)


hoje = date.today()
di, df = hoje.strftime("%d/%m/%Y"), (hoje + timedelta(days=365 * 12)).strftime("%d/%m/%Y")
q = urllib.parse.quote
S = "https://www.debentures.com.br/exploreosnd/consultaadados/"
mostra("SND página de eventos", S + "eventosfinanceiros/agenda_f.asp")
for u in (S + f"eventosfinanceiros/agenda_r.asp?op_exc=False&ativo=HAPV15&dt_ini={q(di)}&dt_fim={q(df)}&evento=",
          S + f"eventosfinanceiros/agenda_e.asp?op_exc=False&ativo=HAPV15&dt_ini={q(di)}&dt_fim={q(df)}&evento=",
          S + f"eventosfinanceiros/agenda_e.asp?op_exc=False&ativo=&dt_ini={q(di)}&dt_fim={q((hoje + timedelta(days=40)).strftime('%d/%m/%Y'))}&evento="):
    mostra("SND agenda", u, n=25, tsv="_e.asp" in u)
mostra("SND eventos financeiros (menu)", S + "eventosfinanceiros/eventosfinanceiros.asp")
# ANBIMA Data: página e possíveis APIs do SPA
for u in ("https://data.anbima.com.br/debentures/HAPV15/agenda",
          "https://data.anbima.com.br/debentures/HAPV15/documentos",
          "https://data.anbima.com.br/debentures/HAPV15/caracteristicas"):
    st, b, ct = req(u)
    t = b.decode("utf-8", errors="replace")
    print("=" * 70, f"\n{u} status={st} bytes={len(b)} ctype={ct}")
    for m in sorted(set(re.findall(r'https?://[a-z0-9.-]*anbima[a-z0-9.-]*/[a-zA-Z0-9_/.-]{3,80}', t)))[:25]:
        print("    url:", m)
    for m in sorted(set(re.findall(r'["\'](/[a-z0-9_-]*api[a-zA-Z0-9_/.-]{2,80})["\']', t)))[:25]:
        print("    api:", m)
    js = re.findall(r'src="([^"]+\.js)"', t)
    print("    js:", js[:6])
    for j in js[:4]:
        ju = j if j.startswith("http") else "https://data.anbima.com.br" + (j if j.startswith("/") else "/" + j)
        st2, b2, _ = req(ju)
        tj = b2.decode("utf-8", errors="replace")
        ach = sorted(set(re.findall(r'https?://[a-z0-9.-]+anbima[a-z0-9.-]*\.com\.br[a-zA-Z0-9_/.-]{0,60}', tj)))
        ach2 = sorted(set(re.findall(r'["`](/(?:api|web-bff|v\d)[a-zA-Z0-9_/${}.-]{2,80})["`]', tj)))
        print(f"    {ju[:90]} -> {st2} {len(b2)}b; hosts: {ach[:15]}; paths: {ach2[:30]}")
