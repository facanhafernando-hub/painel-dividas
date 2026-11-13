"""Diagnóstico: chave da API + fontes de escrituras (CVM IPE, ANBIMA Data, SND)."""
import io, json, os, re, zipfile, collections, urllib.request, urllib.error

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def get(u, data=None, headers=None, timeout=90):
    h = {"User-Agent": UA, "Accept": "*/*"}
    h.update(headers or {})
    try:
        r = urllib.request.urlopen(urllib.request.Request(u, data=data, headers=h), timeout=timeout)
        return r.status, r.headers.get("Content-Type"), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type"), e.read()
    except Exception as e:
        return None, None, repr(e).encode()


print("=== 1. CHAVE ANTHROPIC ===")
k = os.environ.get("ANTHROPIC_API_KEY", "")
print("presente:", bool(k), "| tamanho:", len(k), "| prefixo ok:", k.startswith("sk-ant-"))
if k:
    body = json.dumps({"model": "claude-haiku-5-5", "max_tokens": 5,
                       "messages": [{"role": "user", "content": "ok"}]}).encode()
    st, ct, b = get("https://api.anthropic.com/v1/messages", body,
                    {"x-api-key": k, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    print("status:", st, b[:300])

print("\n=== 2. CVM IPE (dados abertos) ===")
st, ct, b = get("https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/IPE/DADOS/ipe_cia_aberta_2026.zip", timeout=180)
print("status", st, len(b))
if st == 200:
    z = zipfile.ZipFile(io.BytesIO(b))
    nome = z.namelist()[0]
    txt = z.read(nome).decode("latin-1").splitlines()
    cab = txt[0].split(";"); print(cab)
    rows = [dict(zip(cab, l.split(";"))) for l in txt[1:]]
    cat = collections.Counter((r.get("Categoria"), r.get("Tipo")) for r in rows)
    for (c, t), n in cat.most_common(200):
        if re.search(r"escritura|deb[eê]nt|securitiza|termo|assembl|aditamento|plano", f"{c} {t}", re.I):
            print(n, "|", c, "|", t)
    for emp in ("CM HOSPITALAR", "HAPVIDA", "ELFA", "AERIS", "OPEA", "VIRGO"):
        sel = [r for r in rows if emp in r.get("Nome_Companhia", "").upper()]
        print(f"\n-- {emp}: {len(sel)} docs")
        for r in sel[:400]:
            if re.search(r"escritura|deb[eê]nt|termo|assembl|aditamento|plano", " ".join(r.values()), re.I):
                print("  ", r.get("Data_Entrega"), "|", r.get("Categoria"), "|", r.get("Tipo"), "|",
                      r.get("Especie"), "|", (r.get("Assunto") or "")[:90], "|", r.get("Link_Download"))

print("\n=== 3. ANBIMA DATA ===")
for u in ["https://data.anbima.com.br/debentures/VVEO15/documentos",
          "https://data-api.prd.anbima.com.br/web-bff/v1/debentures/VVEO15/documentos",
          "https://data-api.prd.anbima.com.br/web-bff/v1/debentures/VVEO15",
          "https://data-api.prd.anbima.com.br/web-bff/v1/documentos?codigo=VVEO15",
          "https://data-api.prd.anbima.com.br/web-bff/v1/certificado-recebiveis/CRA022008NU/documentos"]:
    st, ct, b = get(u, headers={"Origin": "https://data.anbima.com.br", "Referer": "https://data.anbima.com.br/"})
    print(st, ct, u, "\n   ", b[:500])

print("\n=== 4. SND ===")
for u in ["https://www.debentures.com.br/exploreosnd/consultaadados/emissoesdedebentures/caracteristicas_d.asp?tip_deb=publicas&op_exc=False&ativo=VVEO15",
          "https://www.debentures.com.br/exploreosnd/consultaadados/emissoesdedebentures/documentos_r.asp?ativo=VVEO15"]:
    st, ct, b = get(u)
    t = b.decode("cp1252", errors="replace")
    print(st, ct, len(b), u)
    for m in re.findall(r'href="([^"]+)"[^>]*>([^<]{0,80})', t):
        if re.search(r"pdf|escritura|documento|prospecto|aditamento", " ".join(m), re.I):
            print("   ", m)
