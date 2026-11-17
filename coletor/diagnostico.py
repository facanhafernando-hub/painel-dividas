"""Diagnóstico: documentos IPE da Vero S.A. (CNPJ 31.748.174/0001-60) em 2026. Lista todos e baixa o texto dos
ligados a debêntures, AGD, fato relevante e comunicados. Saída em docs/data/_debug/vero_ipe.json."""
import io, json, subprocess, tempfile, urllib.request, zipfile
UA = {"User-Agent": "Mozilla/5.0"}
CNPJ = "31.748.174/0001-60"
def get(u):
    return urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=240).read()
z = zipfile.ZipFile(io.BytesIO(get("https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/IPE/DADOS/ipe_cia_aberta_2026.zip")))
txt = z.read(z.namelist()[0]).decode("latin-1").splitlines()
cab = txt[0].split(";")
rows = [dict(zip(cab, l.split(";"))) for l in txt[1:]]
rows = [r for r in rows if r.get("CNPJ_Companhia") == CNPJ]
rows.sort(key=lambda r: r.get("Data_Entrega", ""))
print(len(rows), "documentos")
out = {"lista": [{k: r.get(k) for k in ("Data_Entrega", "Data_Referencia", "Categoria", "Tipo", "Especie", "Assunto", "Link_Download")} for r in rows], "textos": []}
CHAVES = ("debentur", "assembleia", "agd", "fato relevante", "comunicado", "rating", "aditamento", "termo", "ata", "edital", "resgate", "oferta")
sel = [r for r in rows if any(k in (r.get("Assunto", "") + " " + r.get("Categoria", "") + " " + r.get("Tipo", "")).lower() for k in CHAVES)]
for r in sel[-40:]:
    try:
        b = get(r["Link_Download"])
        with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
            f.write(b); f.flush()
            t = subprocess.run(["pdftotext", "-layout", f.name, "-"], capture_output=True, text=True, timeout=120).stdout
        out["textos"].append({"data": r.get("Data_Entrega"), "assunto": r.get("Assunto"), "categoria": r.get("Categoria"), "texto": t[:6000]})
        print("ok", r.get("Data_Entrega"), r.get("Assunto")[:80])
    except Exception as e:
        print("erro", r.get("Assunto"), e)
import pathlib
p = pathlib.Path("docs/data/_debug"); p.mkdir(parents=True, exist_ok=True)
(p / "vero_ipe.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
print("==== LISTA")
for r in out["lista"]:
    print(r["Data_Entrega"], "|", r["Categoria"], "|", r["Tipo"], "|", r["Assunto"][:120])
print("==== TEXTOS")
for t in out["textos"]:
    print("\n#####", t["data"], t["assunto"][:120])
    print(" ".join(t["texto"].split())[:2500])
