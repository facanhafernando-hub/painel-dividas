"""Diagnóstico 2: cobertura da CVM para a lista-alvo, download de PDF, segredos, token ANBIMA Data."""
import io, json, os, re, zipfile, collections, urllib.request, urllib.error

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def get(u, data=None, headers=None, timeout=120):
    h = {"User-Agent": UA, "Accept": "*/*"}; h.update(headers or {})
    try:
        r = urllib.request.urlopen(urllib.request.Request(u, data=data, headers=h), timeout=timeout)
        return r.status, r.headers.get("Content-Type"), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type"), e.read()
    except Exception as e:
        return None, None, repr(e).encode()


print("=== segredos ===")
for n in ("ANTHROPIC_API_KEY", "ANTHROPIC_KEY", "CLAUDE_API_KEY", "API_KEY", "CLAUDE_KEY"):
    v = os.environ.get(n, ""); print(n, bool(v), len(v), v.startswith("sk-ant-"))

print("\n=== ANBIMA Data env-config ===")
st, ct, b = get("https://data.anbima.com.br/titulos-privados/env-config.js")
print(st, b[:1500].decode("utf-8", "replace"))

alvo = json.load(open("coletor/escrituras_alvo.json"))
dig = lambda s: re.sub(r"\D", "", s or "")
cnpjs = {dig(a["cnpj"]) for a in alvo if a.get("cnpj")}
print("\n=== CVM IPE 2010-2026: cobertura ===", len(cnpjs), "cnpjs-alvo")
docs = collections.defaultdict(list)
secs = collections.Counter()
for ano in range(2010, 2027):
    st, ct, b = get(f"https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/IPE/DADOS/ipe_cia_aberta_{ano}.zip", timeout=240)
    if st != 200:
        print(ano, st); continue
    z = zipfile.ZipFile(io.BytesIO(b)); txt = z.read(z.namelist()[0]).decode("latin-1").splitlines()
    cab = txt[0].split(";")
    for l in txt[1:]:
        r = dict(zip(cab, l.split(";")))
        cat = r.get("Categoria", "")
        if re.search(r"Escritura|Termo de Securitiza|Recupera|Nota Comercial", cat) or (r.get("Tipo") == "AGDEB" and r.get("Especie") == "Ata"):
            c = dig(r.get("CNPJ_Companhia"))
            if c in cnpjs:
                docs[c].append((r["Data_Entrega"], cat, r.get("Tipo"), r.get("Assunto", "")[:80], r["Link_Download"]))
            if "Securitiza" in cat:
                secs[r.get("Nome_Companhia")] += 1
cob = collections.Counter()
for a in alvo:
    c = dig(a.get("cnpj"))
    esc = [d for d in docs.get(c, []) if "Escritura" in d[1]]
    cob["com escritura na CVM" if esc else ("cnpj sem escritura" if c else "sem cnpj")] += 1
print(cob)
print("empresas-alvo com escrituras:", sum(1 for c in cnpjs if any("Escritura" in d[1] for d in docs.get(c, []))), "de", len(cnpjs))
print("securitizadoras com termos na CVM:", secs.most_common(15))
falta = sorted({a["e"] for a in alvo if a.get("cnpj") and not any("Escritura" in d[1] for d in docs.get(dig(a["cnpj"]), []))})
print("sem escritura na CVM (amostra):", falta[:60])

print("\n=== download de um PDF do RAD ===")
u = "https://www.rad.cvm.gov.br/ENET/frmDownloadDocumento.aspx?Tela=ext&descTipo=IPE&CodigoInstituicao=1&numProtocolo=1555405&numSequencia=1080111&numVersao=1"
st, ct, b = get(u)
print(st, ct, len(b), b[:8])
