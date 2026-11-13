"""Diagnóstico: CRA da Klabin (Vert) negociados a ~70% do par. Mostra o que o informe mensal da CVM e a ANBIMA dizem
sobre cada série: quantidade, valor, saldo, taxa, amortizações e eventos. Saída em docs/data/_debug/klabin_cra.json."""
import csv, io, json, sys, urllib.request, zipfile
csv.field_size_limit(10**9)
CODS = ["CRA0210013B", "CRA020002H1", "CRA020002GZ", "CRA019001E7", "CRA019003JY", "CRA022007EP"]
UA = {"User-Agent": "Mozilla/5.0"}
def get(u):
    return urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=180).read()
out = {"cvm": {}, "anbima": []}
z = zipfile.ZipFile(io.BytesIO(get("https://dados.cvm.gov.br/dados/SECURIT/DOC/INF_MENSAL_CRA/DADOS/inf_mensal_cra_2026.zip")))
print("arquivos:", z.namelist())
tabs = {n: list(csv.DictReader(io.StringIO(z.read(n).decode("latin-1")), delimiter=";")) for n in z.namelist()}
cert = {}
for n, rows in tabs.items():
    if "_classe_" in n:
        for r in rows:
            if (r.get("Codigo_CETIP") or "").strip().upper() in CODS:
                cert[r["Codigo_Identificacao_Certificado"]] = r["Codigo_CETIP"].strip().upper()
print("certificados:", cert)
for n, rows in tabs.items():
    sel = [r for r in rows if r.get("Codigo_Identificacao_Certificado") in cert or (r.get("Codigo_CETIP") or "").strip().upper() in CODS]
    if sel:
        # só as duas últimas competências de cada certificado
        sel.sort(key=lambda r: r.get("Data_Referencia") or r.get("Data_Competencia") or "")
        out["cvm"][n] = sel[-24:]
        print(n, len(sel), "colunas:", list(sel[0].keys())[:40])
try:
    txt = get("https://www.anbima.com.br/pt_br/anbima/TaxasCriCraExport/exportarCSV").decode("latin-1")
    for l in txt.splitlines():
        if any(c in l for c in CODS) or l.lower().startswith(("código", "codigo", "emissor", "tipo")):
            out["anbima"].append(l)
except Exception as e:
    out["anbima"].append(f"erro: {e}")
json.dump(out, open("docs/data/_debug/klabin_cra.json", "w"), ensure_ascii=False, indent=1)
print(json.dumps(out["anbima"], ensure_ascii=False, indent=1))
for n, rows in out["cvm"].items():
    print("=====", n)
    for r in rows:
        print(json.dumps({k: v for k, v in r.items() if v not in ("", None)}, ensure_ascii=False))
