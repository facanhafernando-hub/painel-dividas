"""Leitura de escrituras, aditamentos e atas de AGD (CVM/IPE) para extrair o cronograma exato de cada debênture.

Fluxo:
  1. lista-alvo = filtro (%par > 25 e TIR > 20%) + lista de acompanhamento (a partir do catálogo)
  2. documentos de cada emissão no IPE da CVM (dados abertos), por CNPJ e número da emissão
  3. download do PDF no RAD/ENET, extração de texto (pdftotext) e recorte das cláusulas que importam
  4. leitura pelo modelo (API da Anthropic, em lote ou direto), com saída em JSON
  5. conferência contra a agenda do SND e gravação em docs/data/escrituras/

Uso:
  python coletor/escrituras.py --codigos CMPH14,VVEO15 --direto      (teste, chamadas diretas)
  python coletor/escrituras.py --lote --limite-usd 20                  (todos os pendentes, Batch API)
"""
import argparse, collections, hashlib, io, json, os, re, subprocess, sys, time, unicodedata, urllib.error, urllib.request, zipfile
from datetime import date, datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
DADOS = RAIZ / "docs" / "data"
SAIDA = DADOS / "escrituras"
CACHE = RAIZ / "cache"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
MODELO = "claude-sonnet-5-5"
PRECO = {"direto": (2.0, 10.0), "lote": (1.0, 5.0)}      # US$/MTok (entrada, saída) Sonnet 5.5
MAX_SAIDA = 12000
CHAVE = re.sub(r"\s", "", os.environ.get("ANTHROPIC_API_KEY", ""))

CATS_ESCRITURA = ("Escrituras e aditamentos de debêntures", "Termo de Emissão de Nota Comercial")
CAT_RJ = "Informações de Companhias em Recuperação Judicial ou Extrajudicial"


def log(*a):
    print(*a, flush=True)


def get(u, data=None, headers=None, timeout=180, tentativas=5):
    h = {"User-Agent": UA, "Accept": "*/*"}
    h.update(headers or {})
    erro = None
    for i in range(tentativas):
        try:
            r = urllib.request.urlopen(urllib.request.Request(u, data=data, headers=h), timeout=timeout)
            return r.read()
        except urllib.error.HTTPError as e:
            if e.code < 500 and e.code != 429:
                raise
            erro = e
        except Exception as e:
            erro = e
        time.sleep(5 * (i + 1))
    raise RuntimeError(f"falhou: {u[:120]} ({erro!r})")


def dig(s):
    return re.sub(r"\D", "", s or "")


def sem_acento(s):
    return "".join(c for c in unicodedata.normalize("NFD", s or "") if unicodedata.category(c) != "Mn")


# ---------------------------------------------------------------- 1. lista-alvo
def lista_alvo():
    cat = json.load(open(DADOS / "catalogo.json"))["itens"]
    pct = lambda i: i["upp"] if i.get("upp") is not None else i.get("ppa")
    alvo = {}
    for i in cat:
        if i["t"] != "DEB":
            continue          # CRI/CRA ficam para a etapa seguinte (termos de securitização)
        if i.get("w") or ((pct(i) or 0) > 25 and (i.get("utir") or 0) > 20):
            alvo[i["c"]] = i
    emissoes = collections.defaultdict(list)
    for c, i in alvo.items():
        if i.get("cnpj") and i.get("em"):
            emissoes[(dig(i["cnpj"]), str(i["em"]).lstrip("0"))].append(i)
    return emissoes


# ---------------------------------------------------------------- 2. documentos na CVM
def ipe(anos):
    linhas = []
    (CACHE / "ipe").mkdir(parents=True, exist_ok=True)
    for ano in anos:
        p = CACHE / "ipe" / f"{ano}.zip"
        if not p.exists() or ano >= date.today().year:
            try:
                p.write_bytes(get(f"https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/IPE/DADOS/ipe_cia_aberta_{ano}.zip", timeout=240))
            except Exception as e:
                log("IPE", ano, e)
                continue
        z = zipfile.ZipFile(p)
        txt = z.read(z.namelist()[0]).decode("latin-1").splitlines()
        cab = txt[0].split(";")
        linhas += [dict(zip(cab, l.split(";"))) for l in txt[1:]]
    return linhas


ORDINAIS = {"primeira": 1, "segunda": 2, "terceira": 3, "quarta": 4, "quinta": 5, "sexta": 6, "setima": 7,
            "oitava": 8, "nona": 9, "decima": 10, "decima primeira": 11, "decima segunda": 12, "decima terceira": 13,
            "decima quarta": 14, "decima quinta": 15, "decima sexta": 16, "decima setima": 17, "decima oitava": 18}


def emissoes_citadas(txt):
    t = sem_acento(txt).lower()
    ns = {int(m) for m in re.findall(r"(\d{1,2})\s*[ªaº°o]?\s*(?:\([^)]*\)\s*)?emiss", t)}
    for nome, n in ORDINAIS.items():
        if re.search(rf"\b{nome}\s+emiss", t):
            ns.add(n)
    return ns


def documentos(emissoes, linhas):
    cnpjs = {c for c, _ in emissoes}
    por = collections.defaultdict(list)
    for r in linhas:
        c = dig(r.get("CNPJ_Companhia"))
        if c not in cnpjs:
            continue
        cat = r.get("Categoria", "")
        if cat in CATS_ESCRITURA:
            tipo = "escritura"
        elif r.get("Tipo") == "AGDEB" and r.get("Especie") == "Ata":
            tipo = "ata_agd"
        elif cat == CAT_RJ and r.get("Tipo") in ("Plano de Recuperação", "Pedido de homologação de Plano de Recuperação Extrajudicial"):
            tipo = "plano_rj"
        else:
            continue
        ns = emissoes_citadas(r.get("Assunto", ""))
        doc = {"data": r["Data_Entrega"][:10], "tipo": tipo, "assunto": r.get("Assunto", "")[:300],
               "protocolo": r.get("Protocolo_Entrega"), "versao": r.get("Versao"), "url": r["Link_Download"]}
        for (cc, em) in emissoes:
            if cc != c:
                continue
            if tipo == "plano_rj" or int(em) in ns or (not ns and tipo == "escritura"):
                por[(cc, em)].append(doc)
    # remove versões antigas do mesmo protocolo
    for k, ds in por.items():
        ult = {}
        for d in ds:
            if d["protocolo"] not in ult or int(d["versao"] or 0) > int(ult[d["protocolo"]]["versao"] or 0):
                ult[d["protocolo"]] = d
        por[k] = sorted(ult.values(), key=lambda d: d["data"])
    return por


def selecionar(ds):
    """Escritura original + 3 aditamentos mais recentes + atas de AGD posteriores ao último aditamento + plano de RJ/RE."""
    esc = [d for d in ds if d["tipo"] == "escritura"]
    sel = []
    if esc:
        sel.append(esc[0])
        sel += [d for d in esc[-3:] if d is not esc[0]]
    ult = esc[-1]["data"] if esc else "0000"
    sel += [d for d in ds if d["tipo"] == "ata_agd" and d["data"] >= ult][-3:]
    sel += [d for d in ds if d["tipo"] == "plano_rj"][-1:]
    return sorted(sel, key=lambda d: d["data"])


# ---------------------------------------------------------------- 3. download, texto e recorte
def texto_documento(doc):
    (CACHE / "txt").mkdir(parents=True, exist_ok=True)
    p = CACHE / "txt" / f"{doc['protocolo']}_{doc['versao']}.txt"
    if p.exists():
        return p.read_text()
    b = get(doc["url"], timeout=240, tentativas=6)
    pdfs = []
    if b[:2] == b"PK":
        z = zipfile.ZipFile(io.BytesIO(b))
        pdfs = [z.read(n) for n in z.namelist() if n.lower().endswith(".pdf")]
    elif b[:4] == b"%PDF":
        pdfs = [b]
    else:
        raise RuntimeError(f"formato inesperado {b[:16]!r}")
    partes = []
    for pdf in pdfs:
        tmp = CACHE / "tmp.pdf"
        tmp.write_bytes(pdf)
        out = subprocess.run(["pdftotext", "-layout", str(tmp), "-"], capture_output=True, timeout=300)
        partes.append(out.stdout.decode("utf-8", "replace"))
    t = "\n".join(partes)
    p.write_text(t)
    return t


PALAVRAS = r"amortiza|remunera|juros|vencimento|car[eê]ncia|data[s]? de pagamento|cronograma|anexo|atualiza[cç][aã]o monet|incorpora|capitaliza|pr[eê]mio|valor nominal unit|data de emiss|s[eé]rie|resgate|taxa di|ipca|spread|sobretaxa|repactua|percentual"
DATA_RX = re.compile(r"\d{1,2}\s*(?:/|de)\s*(?:\d{1,2}|[a-zç]{4,9})\s*(?:/|de)\s*\d{4}", re.I)


def recortar(t, limite=90000):
    """Mantém blocos ao redor das palavras-chave e as linhas de tabela com datas (anexos de cronograma)."""
    linhas = t.splitlines()
    manter = [False] * len(linhas)
    for i, l in enumerate(linhas):
        if re.search(PALAVRAS, l, re.I):
            for j in range(max(0, i - 3), min(len(linhas), i + 12)):
                manter[j] = True
        if len(DATA_RX.findall(l)) >= 1 and re.search(r"\d+[,.]\d+\s*%|\d+[,.]\d{2,}", l):
            manter[i] = True
    out, ult = [], -2
    for i, l in enumerate(linhas):
        if manter[i] and l.strip():
            if i > ult + 1:
                out.append("[...]")
            out.append(re.sub(r"\s{3,}", "   ", l.strip()))
            ult = i
    s = "\n".join(out)
    if len(s) > limite:
        s = s[:limite // 2] + "\n[... trecho cortado ...]\n" + s[-limite // 2:]
    return s


# ---------------------------------------------------------------- 4. leitura pelo modelo
INSTRUCOES = """Você é analista de crédito e lê documentos de debêntures brasileiras (escritura, aditamentos, atas de AGD e plano de recuperação) para extrair o CRONOGRAMA VIGENTE HOJE de cada série.

Regras:
- Os documentos vêm em ordem cronológica. Termos de aditamento, ata de AGD ou plano posteriores PREVALECEM sobre os anteriores. Se um aditamento consolidou a escritura, use a versão consolidada.
- Os trechos foram recortados automaticamente: "[...]" indica texto omitido.
- Extraia somente o que estiver escrito. Não invente. Se algo não estiver nos trechos, use null e explique em "lacunas".
- Percentuais de amortização: informe exatamente como no documento e diga a base: "vne" (% do Valor Nominal Unitário original/da emissão), "saldo" (% do saldo do Valor Nominal Unitário/atualizado na data) ou "desconhecida".
- Datas no formato AAAA-MM-DD.
- Para juros com periodicidade regular, informe a regra (periodicidade em meses, primeira e última data); liste datas avulsas só se forem irregulares.
- Incorporação de juros (PIK/capitalização) e carência de juros também devem ser registradas com as datas.
- Se houver vencimento antecipado declarado, suspensão de pagamentos, RJ/RE ou proposta de novação, registre em "situacao".

Responda APENAS com um JSON válido, sem texto fora dele, no formato:
{
 "series": [
  {"codigo": "XXXX11", "serie": "1", "data_emissao": "AAAA-MM-DD", "vencimento": "AAAA-MM-DD", "vne": 1000.0,
   "remuneracao": {"indexador": "DI+" | "%DI" | "IPCA+" | "IGPM+" | "PRE" | "outro", "taxa": 2.5, "base": "252 du" | "360" | "365", "vigente_desde": "AAAA-MM-DD" | null, "historico": "texto curto se mudou"},
   "juros": {"periodicidade_meses": 6, "primeira_data": "AAAA-MM-DD", "ultima_data": "AAAA-MM-DD", "datas_irregulares": ["AAAA-MM-DD"], "carencia_ate": "AAAA-MM-DD" | null},
   "incorporacao_juros": [{"de": "AAAA-MM-DD", "ate": "AAAA-MM-DD", "observacao": ""}],
   "amortizacoes": [{"data": "AAAA-MM-DD", "percentual": 10.0, "base": "vne" | "saldo" | "desconhecida"}],
   "premios": [{"data": "AAAA-MM-DD", "descricao": ""}],
   "fonte": "documento e cláusula/anexo de onde saiu o cronograma",
   "confianca": "alta" | "media" | "baixa"}
 ],
 "situacao": "texto curto (vencimento antecipado, suspensão, RJ/RE, renegociação em curso) ou null",
 "lacunas": "o que não foi possível determinar e por quê, ou null"
}"""


def montar_prompt(emissao, itens, docs_txt):
    cods = ", ".join(f"{i['c']} (série {i.get('se') or '?'})" for i in itens)
    cab = f"Emissora: {itens[0]['e']} (CNPJ {emissao[0]}), {emissao[1]}ª emissão de debêntures.\nSéries a extrair (códigos B3/CETIP): {cods}.\n"
    corpo = "\n\n".join(f"===== DOCUMENTO {n + 1}: {d['tipo']} entregue em {d['data']} | {d['assunto'][:200]} =====\n{t}"
                        for n, (d, t) in enumerate(docs_txt))
    return cab + "\n" + corpo


def api(caminho, corpo=None, metodo=None):
    h = {"x-api-key": CHAVE, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    req = urllib.request.Request("https://api.anthropic.com" + caminho, headers=h, method=metodo,
                                 data=json.dumps(corpo).encode() if corpo is not None else None)
    for i in range(6):
        try:
            return urllib.request.urlopen(req, timeout=600).read()
        except urllib.error.HTTPError as e:
            msg = e.read()[:400]
            if e.code in (429, 500, 502, 503, 529):
                time.sleep(20 * (i + 1)); continue
            raise RuntimeError(f"API {e.code}: {msg}")
    raise RuntimeError("API indisponível")


def params(prompt):
    return {"model": MODELO, "max_tokens": MAX_SAIDA, "system": INSTRUCOES,
            "messages": [{"role": "user", "content": prompt}]}


def ler_json(txt):
    m = re.search(r"\{.*\}", txt, re.S)
    return json.loads(m.group(0)) if m else None


def custo(uso, modo):
    pe, ps = PRECO[modo]
    return (uso.get("input_tokens", 0) * pe + uso.get("output_tokens", 0) * ps) / 1e6


# ---------------------------------------------------------------- 5. conferência com a agenda do SND
def conferir(serie):
    p = DADOS / "cronograma" / f"{serie.get('codigo')}.json"
    if not p.exists():
        return {"snd": False}
    ev = json.load(open(p)).get("ev", [])
    def iso(d):
        try:
            return datetime.strptime(d, "%d/%m/%Y").strftime("%Y-%m-%d")
        except Exception:
            return d
    snd = {iso(e[0]): e[2] for e in ev if e[1] in ("A", "V")}
    doc = {a["data"]: a.get("percentual") for a in serie.get("amortizacoes") or [] if a.get("data")}
    comuns = set(snd) & set(doc)
    dif_pct = sorted(d for d in comuns if snd[d] is not None and doc[d] is not None and abs(float(snd[d]) - float(doc[d])) > 0.01)
    return {"snd": True, "amort_snd": len(snd), "amort_doc": len(doc), "datas_iguais": len(comuns),
            "so_snd": sorted(set(snd) - set(doc))[:12], "so_doc": sorted(set(doc) - set(snd))[:12], "pct_diferente": dif_pct[:12],
            "ok": len(comuns) == len(snd) == len(doc) and not dif_pct}


# ---------------------------------------------------------------- orquestração
def chave_emissao(e):
    return f"{e[0]}_{e[1]}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codigos", default="")
    ap.add_argument("--direto", action="store_true")
    ap.add_argument("--lote", action="store_true")
    ap.add_argument("--limite-usd", type=float, default=20.0)
    ap.add_argument("--forcar", action="store_true")
    a = ap.parse_args()
    if not CHAVE:
        sys.exit("ANTHROPIC_API_KEY ausente")
    SAIDA.mkdir(parents=True, exist_ok=True)
    idx_p = SAIDA / "_index.json"
    idx = json.load(open(idx_p)) if idx_p.exists() else {"emissoes": {}, "gasto_usd": 0.0}

    emissoes = lista_alvo()
    if a.codigos:
        cods = {c.strip().upper() for c in a.codigos.split(",")}
        emissoes = {k: v for k, v in emissoes.items() if any(i["c"] in cods for i in v)}
    log("emissões-alvo:", len(emissoes))
    linhas = ipe(range(2010, date.today().year + 1))
    docs = documentos(emissoes, linhas)

    pendentes = []
    for e, itens in sorted(emissoes.items()):
        k = chave_emissao(e)
        sel = selecionar(docs.get(e, []))
        assinatura = hashlib.sha1(json.dumps([d["protocolo"] + "_" + str(d["versao"]) for d in sel]).encode()).hexdigest()[:12]
        if not sel:
            idx["emissoes"][k] = {"codigos": [i["c"] for i in itens], "status": "sem_documentos_cvm"}
            continue
        if not a.forcar and idx["emissoes"].get(k, {}).get("assinatura") == assinatura and idx["emissoes"][k].get("status") == "ok":
            continue
        docs_txt = []
        for d in sel:
            try:
                docs_txt.append((d, recortar(texto_documento(d))))
            except Exception as ex:
                log("  download falhou", k, d["protocolo"], ex)
        if not docs_txt or sum(len(t) for _, t in docs_txt) < 500:
            idx["emissoes"][k] = {"codigos": [i["c"] for i in itens], "status": "texto_vazio", "docs": sel}
            continue
        prompt = montar_prompt(e, itens, docs_txt)
        pendentes.append((k, e, itens, sel, assinatura, prompt))
        log(f"  {k} {[i['c'] for i in itens]}: {len(sel)} docs, {len(prompt):,} caracteres")

    modo = "lote" if a.lote else "direto"
    est = sum(len(p[5]) / 3.2 for p in pendentes)
    est_usd = (est * PRECO[modo][0] + len(pendentes) * 4000 * PRECO[modo][1]) / 1e6
    log(f"pendentes: {len(pendentes)} | ~{est / 1e6:.2f} MTok de entrada | custo estimado US$ {est_usd:.2f} | já gasto US$ {idx['gasto_usd']:.2f}")
    if idx["gasto_usd"] + est_usd > a.limite_usd:
        sys.exit(f"ABORTADO: estimativa passaria do teto de US$ {a.limite_usd}")
    if not pendentes:
        json.dump(idx, open(idx_p, "w"), ensure_ascii=False, indent=1)
        return

    resultados = {}
    if modo == "direto":
        for k, e, itens, sel, ass, prompt in pendentes:
            r = json.loads(api("/v1/messages", params(prompt)))
            resultados[k] = ("".join(b.get("text", "") for b in r["content"]), r.get("usage", {}))
    else:
        req = [{"custom_id": p[0], "params": params(p[5])} for p in pendentes]
        b = json.loads(api("/v1/messages/batches", {"requests": req}))
        idx["lote_em_andamento"] = b["id"]
        json.dump(idx, open(idx_p, "w"), ensure_ascii=False, indent=1)
        log("lote enviado:", b["id"])
        t0 = time.time()
        while True:
            st = json.loads(api(f"/v1/messages/batches/{b['id']}"))
            log("  status:", st["processing_status"], st.get("request_counts"))
            if st["processing_status"] == "ended":
                break
            if time.time() - t0 > 2 * 3600:
                sys.exit("lote ainda em processamento; rode de novo para coletar")
            time.sleep(60)
        for l in api(f"/v1/messages/batches/{b['id']}/results").decode().splitlines():
            r = json.loads(l)
            if r["result"]["type"] == "succeeded":
                m = r["result"]["message"]
                resultados[r["custom_id"]] = ("".join(x.get("text", "") for x in m["content"]), m.get("usage", {}))
            else:
                log("  falhou:", r["custom_id"], r["result"])
        idx.pop("lote_em_andamento", None)

    for k, e, itens, sel, ass, prompt in pendentes:
        if k not in resultados:
            continue
        txt, uso = resultados[k]
        idx["gasto_usd"] = round(idx["gasto_usd"] + custo(uso, modo), 4)
        try:
            j = ler_json(txt)
        except Exception:
            j = None
        if not j:
            idx["emissoes"][k] = {"codigos": [i["c"] for i in itens], "status": "json_invalido", "assinatura": ass}
            (SAIDA / f"{k}.raw.txt").write_text(txt)
            continue
        for s in j.get("series", []):
            s["conferencia_snd"] = conferir(s)
        j["meta"] = {"emissora": itens[0]["e"], "cnpj": e[0], "emissao": e[1], "lido_em": date.today().isoformat(),
                     "modelo": MODELO, "documentos": sel, "uso": uso}
        json.dump(j, open(SAIDA / f"{k}.json", "w"), ensure_ascii=False, indent=1)
        oks = [s["conferencia_snd"].get("ok") for s in j.get("series", [])]
        idx["emissoes"][k] = {"codigos": [i["c"] for i in itens], "status": "ok", "assinatura": ass,
                              "bate_snd": all(oks) if oks else None, "situacao": j.get("situacao")}
        log(f"  {k}: {len(j.get('series', []))} séries | bate com SND: {oks} | situação: {j.get('situacao')}")
    json.dump(idx, open(idx_p, "w"), ensure_ascii=False, indent=1)
    log(f"gasto acumulado: US$ {idx['gasto_usd']:.2f}")


if __name__ == "__main__":
    main()
