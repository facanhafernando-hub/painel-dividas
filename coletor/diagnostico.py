"""Diagnóstico das fontes: roda no GitHub e imprime o que cada endpoint devolve."""
import collections, csv, io, json, sys, time, urllib.request, urllib.error
from datetime import date, timedelta

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def req(url, corpo=None, timeout=90, hdr=None):
    h = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "pt-BR,pt;q=0.9"}
    if corpo is not None:
        h["Content-Type"] = "application/json"
    h.update(hdr or {})
    t = time.time()
    try:
        r = urllib.request.urlopen(urllib.request.Request(url, data=corpo, headers=h), timeout=timeout)
        b = r.read()
        return r.status, dict(r.headers), b, time.time() - t
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()[:500], time.time() - t
    except Exception as e:
        return None, {}, repr(e).encode(), time.time() - t


def uteis(n):
    d, out = date.today(), []
    while len(out) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            out.append(d)
    return out


ref = uteis(1)[0]
print("=" * 80, "\nB3 export CSV, ConsolidatedRecords", ref)
for lang in ("pt-br", "en-us"):
    st, h, b, dt = req(f"https://arquivos.b3.com.br/bdi/table/export/csv?lang={lang}",
                       json.dumps({"Name": "ConsolidatedRecords", "Date": ref.isoformat(),
                                   "FinalDate": ref.isoformat()}).encode())
    print(f"[{lang}] status={st} bytes={len(b)} t={dt:.1f}s ctype={h.get('Content-Type')}")
    if st == 200 and b:
        txt = None
        for enc in ("utf-8-sig", "latin-1"):
            try:
                txt = b.decode(enc); break
            except UnicodeDecodeError:
                pass
        linhas = txt.splitlines()
        print("primeiras linhas:")
        for l in linhas[:8]:
            print("   ", l[:400])
        print("total linhas:", len(linhas))
        # tenta achar cabeçalho e contar tipos
        for i, l in enumerate(linhas[:10]):
            if l.count(";") > 5 or l.count(",") > 5:
                sep = ";" if l.count(";") > l.count(",") else ","
                rd = list(csv.reader(linhas[i:], delimiter=sep))
                cab = rd[0]
                print("cabeçalho:", cab)
                for j, nome in enumerate(cab):
                    vals = [r[j] for r in rd[1:] if len(r) > j]
                    c = collections.Counter(vals)
                    if 1 < len(c) <= 60:
                        print(f"  coluna '{nome}': {c.most_common(25)}")
                break
    else:
        print("   corpo:", b[:300])

print("=" * 80, "\nB3 JSON (página 1, 5 linhas)")
st, h, b, dt = req(f"https://arquivos.b3.com.br/bdi/table/ConsolidatedRecords/{ref}/{ref}/1/5", b"{}")
print(f"status={st} t={dt:.1f}s")
try:
    t = json.loads(b)["table"]
    print("columns:", json.dumps(t.get("columns"), ensure_ascii=False)[:2500])
    for v in t.get("values", [])[:5]:
        print("  ", v)
    print("outras chaves:", {k: v for k, v in json.loads(b).items() if k != "table"})
except Exception as e:
    print("não-JSON:", b[:300], e)

print("=" * 80, "\nANBIMA")
for d in uteis(4):
    u = f"https://www.anbima.com.br/informacoes/merc-sec-debentures/arqs/db{d:%y%m%d}.txt"
    st, h, b, dt = req(u)
    print(f"{u} -> status={st} bytes={len(b)} t={dt:.1f}s server={h.get('Server')} ctype={h.get('Content-Type')}")
    print("   ", b[:300])
for u in ("https://www.anbima.com.br/informacoes/merc-sec-debentures/default.asp",
          "https://www.anbima.com.br/informacoes/merc-sec-debentures/",
          "https://www.anbima.com.br/pt_br/informar/precos-e-indices/precos/taxas-debentures.htm",
          "https://data.anbima.com.br/"):
    st, h, b, dt = req(u)
    print(f"{u} -> status={st} bytes={len(b)} t={dt:.1f}s server={h.get('Server')}")
    if st == 200:
        import re
        links = sorted(set(re.findall(rb'href="([^"]*(?:\.txt|\.xls|\.csv|resultados)[^"]*)"', b)))[:15]
        print("    links:", links)
