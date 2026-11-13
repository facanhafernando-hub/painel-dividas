"""Diagnóstico: documentos da 6ª emissão da BCBF (BCBF16) na CVM: garantias e remuneração."""
import io, re, subprocess, time, zipfile, urllib.request
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
CNPJS = {"19276528000116": "BCBF", "05197443000138": "HAPVIDA"}


def get(u, t=240):
    for i in range(5):
        try:
            return urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": UA}), timeout=t).read()
        except Exception as e:
            err = e; time.sleep(6 * (i + 1))
    raise err


docs = []
for ano in range(2021, 2027):
    z = zipfile.ZipFile(io.BytesIO(get(f"https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/IPE/DADOS/ipe_cia_aberta_{ano}.zip")))
    txt = z.read(z.namelist()[0]).decode("latin-1").splitlines()
    cab = txt[0].split(";")
    for l in txt[1:]:
        r = dict(zip(cab, l.split(";")))
        c = re.sub(r"\D", "", r.get("CNPJ_Companhia", ""))
        if c not in CNPJS:
            continue
        tudo = " ".join([r.get("Categoria", ""), r.get("Tipo", ""), r.get("Especie", ""), r.get("Assunto", "")])
        if re.search(r"escritura|aditamento|AGDEB|debenturista", tudo, re.I) and (
                CNPJS[c] == "BCBF" and re.search(r"6|sexta", r.get("Assunto", ""), re.I) or
                CNPJS[c] == "HAPVIDA" and re.search(r"BCBF", r.get("Assunto", ""), re.I)):
            docs.append((r["Data_Entrega"][:10], CNPJS[c], r.get("Categoria", "")[:40], r.get("Tipo", ""), r.get("Assunto", "")[:160], r["Link_Download"]))
docs.sort()
print("DOCUMENTOS:", len(docs))
for d in docs:
    print(" ", d[:5])

PAL = re.compile(r"fidejuss|fian[çc]a|fiador|garantia real|garantias?\b|aliena[çc][ãa]o fiduci|cess[ãa]o fiduci|esp[ée]cie|sobretaxa|spread|1,45|1,60|remunera[çc][ãa]o", re.I)
for d in docs:
    try:
        b = get(d[5])
    except Exception as e:
        print("\n### FALHOU", d[:2], e); continue
    pdfs = [b] if b[:4] == b"%PDF" else ([zipfile.ZipFile(io.BytesIO(b)).read(n) for n in zipfile.ZipFile(io.BytesIO(b)).namelist() if n.lower().endswith(".pdf")] if b[:2] == b"PK" else [])
    texto = ""
    for p in pdfs:
        open("/tmp/x.pdf", "wb").write(p)
        texto += subprocess.run(["pdftotext", "-layout", "/tmp/x.pdf", "-"], capture_output=True).stdout.decode("utf-8", "replace")
    print(f"\n### {d[0]} {d[1]} | {d[3]} | {d[4]} | {len(texto)} caracteres")
    linhas = texto.splitlines()
    vistos = set()
    for i, l in enumerate(linhas):
        if PAL.search(l) and i not in vistos:
            bloco = " ".join(x.strip() for x in linhas[max(0, i - 1):i + 3])
            for j in range(i - 1, i + 3):
                vistos.add(j)
            print("   >", re.sub(r"\s+", " ", bloco)[:420])
