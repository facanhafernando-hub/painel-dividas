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
    ns = set()
    for g in re.findall(r"((?:\d{1,2}\s*[ªaº°o]?\s*(?:\([^)]*\))?\s*(?:,|e)\s*)*\d{1,2}\s*[ªaº°o]?\s*(?:\([^)]*\)\s*)?)emiss", t):
        ns |= {int(n) for n in re.findall(r"\d{1,2}", re.sub(r"\([^)]*\)", "", g))}
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
        elif r.get("Tipo") == "AGDEB" and r.get("Especie", "").startswith("Proposta"):
            tipo = "proposta_agd"
        elif cat in ("Fato Relevante", "Comunicado ao Mercado") and re.search(
                r"deb[eê]nt|vencimento antecipado|recupera[cç][aã]o|assembleia[s]? gera(l|is) de debenturistas|\bagd|reperfil|repactua|reestrutura",
                sem_acento(r.get("Assunto", "")), re.I):
            tipo = "comunicado"
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
            if tipo == "plano_rj" or int(em) in ns or not ns:
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
    recente = (date.today().replace(year=date.today().year - 2)).isoformat()
    sel += [d for d in ds if d["tipo"] == "ata_agd" and d["data"] >= ult][-4:]
    sel += [d for d in ds if d["tipo"] == "proposta_agd" and d["data"] >= ult][-2:]
    sel += [d for d in ds if d["tipo"] == "comunicado" and d["data"] >= max(ult, recente)][-3:]
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
        tmp = CACHE / f"tmp_{doc['protocolo']}_{len(partes)}.pdf"
        tmp.write_bytes(pdf)
        out = subprocess.run(["pdftotext", "-layout", str(tmp), "-"], capture_output=True, timeout=300)
        partes.append(out.stdout.decode("utf-8", "replace"))
        tmp.unlink(missing_ok=True)
    t = "\n".join(partes)
    p.write_text(t)
    return t


FORTE = r"amortiza|cronograma|vencimento|remunera|car[eê]ncia|incorpora|capitaliza|data[s]? de pagamento|repactua|spread|sobretaxa|juros remunerat|anexo"
FRACA = r"juros|atualiza[cç][aã]o monet|pr[eê]mio|valor nominal unit|data de emiss|s[eé]rie|resgate|taxa di|ipca|percentual|vencimento antecipado|suspens"
DATA_RX = re.compile(r"\d{1,2}\s*(?:/|de)\s*(?:\d{1,2}|[a-zç]{4,9})\s*(?:/|de)\s*\d{4}", re.I)
LIMITE_TIPO = {"escritura": 90000, "ata_agd": 40000, "proposta_agd": 45000, "comunicado": 10000, "plano_rj": 70000}


def recortar(t, limite=90000):
    """Prioridade: (1) linhas de tabela com data e número (cronogramas), (2) cláusulas de amortização/remuneração/vencimento,
    (3) demais cláusulas relevantes. Corta das camadas menos importantes até caber no limite, sem quebrar tabelas."""
    linhas = t.splitlines()
    n = len(linhas)
    tabela = [bool(DATA_RX.search(l)) and bool(re.search(r"\d+[,.]\d+", l)) for l in linhas]
    def janelas(rx, antes, depois):
        m = [False] * n
        for i, l in enumerate(linhas):
            if re.search(rx, l, re.I):
                for j in range(max(0, i - antes), min(n, i + depois)):
                    m[j] = True
        return m
    for forte, fraca in ((janelas(FORTE, 3, 14), janelas(FRACA, 1, 5)), (janelas(FORTE, 2, 8), [False] * n), (janelas(FORTE, 1, 4), [False] * n)):
        manter = [tabela[i] or forte[i] or fraca[i] for i in range(n)]
        out, ult = [], -2
        for i, l in enumerate(linhas):
            if manter[i] and l.strip():
                if i > ult + 1:
                    out.append("[...]")
                out.append(re.sub(r"\s{3,}", "   ", l.strip()))
                ult = i
        s = "\n".join(out)
        if len(s) <= limite:
            return s
    return s[:int(limite * 0.6)] + "\n[... trecho cortado ...]\n" + s[-int(limite * 0.4):]


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
  {"codigo": "XXXX11", "emissao": 1, "serie": "1", "data_emissao": "AAAA-MM-DD", "vencimento": "AAAA-MM-DD", "vne": 1000.0,
   "remuneracao": {"indexador": "DI+" | "%DI" | "IPCA+" | "IGPM+" | "PRE" | "outro", "taxa": 2.5, "base": "252 du" | "360" | "365", "vigente_desde": "AAAA-MM-DD" | null, "historico": "texto curto se mudou"},
   "juros": {"periodicidade_meses": 6, "primeira_data": "AAAA-MM-DD", "ultima_data": "AAAA-MM-DD", "datas_irregulares": ["AAAA-MM-DD"], "carencia_ate": "AAAA-MM-DD" | null},
   "incorporacao_juros": [{"de": "AAAA-MM-DD", "ate": "AAAA-MM-DD", "observacao": ""}],
   "amortizacoes": [{"data": "AAAA-MM-DD", "percentual": 10.0, "base": "vne" | "saldo" | "desconhecida"}],
   "premios": [{"data": "AAAA-MM-DD", "descricao": ""}],
   "fonte": "documento e cláusula/anexo de onde saiu o cronograma",
   "confianca": "alta" | "media" | "baixa"}
 ],
 "situacao": "texto curto por emissão (vencimento antecipado, suspensão, RJ/RE, renegociação em curso) ou null",
 "lacunas": "o que não foi possível determinar e por quê, ou null"
}"""


def montar_prompt(grupo, docs_txt):
    cnpj = grupo[0][0][0]
    linhas = [f"Emissora: {grupo[0][1][0]['e']} (CNPJ {cnpj})."]
    for (c, em), itens in grupo:
        linhas.append(f"- {em}ª emissão: séries " + ", ".join(f"{i['c']} (série {i.get('se') or '?'})" for i in itens))
    cab = "Extraia o cronograma de TODAS as séries abaixo (cada documento pode tratar de uma ou mais emissões):\n" + "\n".join(linhas) + "\n"
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
    hoje = date.today().isoformat()
    snd = {iso(e[0]): e[2] for e in ev if e[1] == "A" and (e[2] or 0) > 0}
    for e in ev:
        if e[1] == "V":
            snd.setdefault(iso(e[0]), 100.0)
    snd = {d: v for d, v in snd.items() if d >= hoje}
    doc = {a["data"]: a.get("percentual") for a in serie.get("amortizacoes") or [] if a.get("data") and a["data"] >= hoje}
    par, livres = {}, set(snd)
    for d in sorted(doc):          # casa cada data do documento com a do SND (que já vem ajustada para dia útil)
        try:
            dd = datetime.strptime(d, "%Y-%m-%d")
        except Exception:
            continue
        cand = [x for x in livres if abs((datetime.strptime(x, "%Y-%m-%d") - dd).days) <= 5]
        if cand:
            x = min(cand, key=lambda x: abs((datetime.strptime(x, "%Y-%m-%d") - dd).days))
            par[d] = x; livres.discard(x)
    dif_pct = sorted(d for d, x in par.items() if snd[x] is not None and doc[d] is not None and abs(float(snd[x]) - float(doc[d])) > 0.05)
    return {"snd": True, "amort_snd": len(snd), "amort_doc": len(doc), "datas_iguais": len(par),
            "so_snd": sorted(livres)[:12], "so_doc": sorted(set(doc) - set(par))[:12], "pct_diferente": dif_pct[:12],
            "ok": len(par) == len(snd) == len(doc) and not dif_pct}


def processar(idx, pendentes, resultados, modo):
    for k, grupo, sel, ass in pendentes:
        if k not in resultados:
            continue
        txt, uso = resultados[k]
        idx["gasto_usd"] = round(idx["gasto_usd"] + custo(uso, modo), 4)
        try:
            j = ler_json(txt)
        except Exception:
            j = None
        if not j:
            for e, itens in grupo:
                idx["emissoes"][chave_emissao(e)] = {"codigos": [i["c"] for i in itens], "status": "json_invalido", "assinatura": ass}
            (SAIDA / f"{k}.raw.txt").write_text(txt)
            continue
        for e, itens in grupo:
            cods = {i["c"] for i in itens}
            series = [s for s in j.get("series", []) if s.get("codigo") in cods or str(s.get("emissao")) == e[1]]
            for s in series:
                s["conferencia_snd"] = conferir(s)
            out = {"series": series, "situacao": j.get("situacao"), "lacunas": j.get("lacunas"),
                   "meta": {"emissora": itens[0]["e"], "cnpj": e[0], "emissao": e[1], "lido_em": date.today().isoformat(),
                            "modelo": MODELO, "documentos": sel, "uso_grupo": uso, "grupo": k}}
            json.dump(out, open(SAIDA / f"{chave_emissao(e)}.json", "w"), ensure_ascii=False, indent=1)
            oks = [s["conferencia_snd"].get("ok") for s in series]
            idx["emissoes"][chave_emissao(e)] = {"codigos": sorted(cods), "status": "ok" if series else "sem_series", "assinatura": ass,
                                                 "bate_snd": all(oks) if oks else None}
            log(f"  {chave_emissao(e)}: {len(series)} séries | bate com SND: {oks}")


def coletar_lote(idx, idx_p, espera_max=2 * 3600):
    lote = idx["lote"]
    t0 = time.time()
    while True:
        st = json.loads(api(f"/v1/messages/batches/{lote['id']}"))
        log("  lote", lote["id"], st["processing_status"], st.get("request_counts"))
        if st["processing_status"] == "ended":
            break
        if time.time() - t0 > espera_max:
            return False
        time.sleep(60)
    resultados = {}
    for l in api(f"/v1/messages/batches/{lote['id']}/results").decode().splitlines():
        r = json.loads(l)
        if r["result"]["type"] == "succeeded":
            m = r["result"]["message"]
            resultados[r["custom_id"]] = ("".join(x.get("text", "") for x in m["content"]), m.get("usage", {}))
        else:
            log("  falhou:", r["custom_id"], str(r["result"])[:300])
    pend = [(k, [((c, em), [{"c": x, "e": emp} for x in cods]) for c, em, cods, emp in v["grupo"]], v["sel"], v["ass"])
            for k, v in lote["itens"].items()]
    processar(idx, pend, resultados, "lote")
    idx.pop("lote", None)
    return True


CLASSIFICA = """Você recebe o resumo da situação de uma emissão de debêntures, escrito por um analista a partir de documentos da CVM. O resumo pode mencionar outras emissões da mesma empresa.
Para CADA código listado, classifique o evento de crédito VIGENTE que afeta aquela série (ou a emissora como um todo):
- "vencimento_antecipado": vencimento antecipado declarado ou automático já ocorrido (não conta mera previsão contratual nem AGD que decidiu NÃO declarar)
- "rj": emissora ou fiadora em recuperação judicial em curso
- "re": recuperação extrajudicial pedida ou homologada
- "reperfilamento": cronograma/remuneração alterados por renegociação, waiver com mudança de fluxo, suspensão temporária de pagamentos ou plano já implementado
- "nenhum": nada disso
Se houver mais de um, use o mais grave (vencimento_antecipado > rj > re > reperfilamento). Frases como "não há vencimento antecipado" significam "nenhum".
Responda só com JSON: {"CODIGO": {"evento": "...", "data": "AAAA-MM-DD ou null", "resumo": "até 160 caracteres, em português, sem travessão"}}"""


def classificar(forcar=False):
    """Converte o texto livre de situação em evento estruturado por código (modelo pequeno, custo de centavos)."""
    from concurrent.futures import ThreadPoolExecutor
    arqs = [p for p in SAIDA.glob("*_*.json") if not p.name.startswith("_")]
    fila = []
    for p in arqs:
        j = json.load(open(p))
        cods = [s.get("codigo") for s in j.get("series", []) if s.get("codigo")]
        if not cods or (all(c in (j.get("eventos") or {}) for c in cods) and not forcar):
            continue
        fila.append((p, j, cods))
    gasto = 0.0
    def um(x):
        p, j, cods = x
        txt = f"Emissão {j['meta']['emissao']}ª de {j['meta']['emissora']}. Códigos: {', '.join(cods)}.\nResumo da situação: {j.get('situacao') or 'sem informação'}"
        out, c = {}, 0.0
        for _ in range(2):
            try:
                r = json.loads(api("/v1/messages", {"model": "claude-haiku-5-5", "max_tokens": 1500, "system": CLASSIFICA,
                                                    "messages": [{"role": "user", "content": txt}]}))
                u = r.get("usage", {})
                c += (u.get("input_tokens", 0) * 0.10 + u.get("output_tokens", 0) * 0.50) / 1e6
                out = ler_json("".join(b.get("text", "") for b in r["content"])) or {}
                break
            except Exception as ex:
                log("  classificação falhou", p.name, str(ex)[:120])
        return p, j, out, c
    with ThreadPoolExecutor(6) as ex:
        for p, j, out, c in ex.map(um, fila):
            gasto += c
            j["eventos"] = {k: v for k, v in out.items() if isinstance(v, dict)}
            json.dump(j, open(p, "w"), ensure_ascii=False, indent=1)
    log(f"classificadas: {len(fila)} emissões | US$ {gasto:.3f}")
    return gasto


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
    ap.add_argument("--classificar", action="store_true", help="só classifica as situações já lidas")
    a = ap.parse_args()
    if not CHAVE:
        sys.exit("ANTHROPIC_API_KEY ausente")
    SAIDA.mkdir(parents=True, exist_ok=True)
    idx_p = SAIDA / "_index.json"
    idx = json.load(open(idx_p)) if idx_p.exists() else {"emissoes": {}, "gasto_usd": 0.0}

    if a.classificar:
        idx["gasto_usd"] = round(idx["gasto_usd"] + classificar(a.forcar), 4)
        json.dump(idx, open(idx_p, "w"), ensure_ascii=False, indent=1)
        return
    if idx.get("lote"):
        log("coletando lote pendente", idx["lote"]["id"])
        ok = coletar_lote(idx, idx_p)
        json.dump(idx, open(idx_p, "w"), ensure_ascii=False, indent=1)
        if not ok:
            sys.exit("lote anterior ainda em processamento")
    emissoes = lista_alvo()
    if a.codigos:
        cods = {c.strip().upper() for c in a.codigos.split(",")}
        emissoes = {k: v for k, v in emissoes.items() if any(i["c"] in cods for i in v)}
    log("emissões-alvo:", len(emissoes))
    linhas = ipe(range(2010, date.today().year + 1))
    docs = documentos(emissoes, linhas)

    # agrupa por empresa: documentos compartilhados (atas, comunicados, planos) são lidos uma vez só
    # baixa e extrai em paralelo tudo o que vai ser lido (fica em cache entre execuções)
    from concurrent.futures import ThreadPoolExecutor
    todos = {d["protocolo"]: d for e in emissoes for d in selecionar(docs.get(e, []))}
    log("documentos a baixar/extrair:", len(todos))
    def baixa(d):
        try:
            texto_documento(d)
        except Exception as ex:
            return d["protocolo"], str(ex)[:120]
        return d["protocolo"], None
    with ThreadPoolExecutor(8) as ex:
        falhas = [r for r in ex.map(baixa, todos.values()) if r[1]]
    log("falhas de download:", len(falhas), falhas[:5])
    por_cnpj = collections.defaultdict(list)
    for e, itens in sorted(emissoes.items()):
        por_cnpj[e[0]].append((e, itens))
    pendentes = []
    for cnpj, ems in por_cnpj.items():
        sel_por = {e: selecionar(docs.get(e, [])) for e, _ in ems}
        for e, itens in ems:
            if not sel_por[e]:
                idx["emissoes"][chave_emissao(e)] = {"codigos": [i["c"] for i in itens], "status": "sem_documentos_cvm"}
        ems = [x for x in ems if sel_por[x[0]]]
        txt = {}
        for e, _ in ems:
            for d in sel_por[e]:
                if d["protocolo"] in txt:
                    continue
                try:
                    txt[d["protocolo"]] = recortar(texto_documento(d), LIMITE_TIPO.get(d["tipo"], 60000))
                except Exception as ex:
                    txt[d["protocolo"]] = ""
                    log("  download falhou", cnpj, d["protocolo"], ex)
        # fatia em grupos que caibam num pedido (~330 mil caracteres de documentos distintos)
        grupos, atual, vistos = [], [], set()
        for e, itens in ems:
            novos = {d["protocolo"] for d in sel_por[e]} - vistos
            if atual and sum(len(txt[p]) for p in vistos | novos) > 330000:
                grupos.append(atual); atual, vistos = [], set()
                novos = {d["protocolo"] for d in sel_por[e]}
            atual.append((e, itens)); vistos |= novos
        if atual:
            grupos.append(atual)
        for grupo in grupos:
            vistos, sel = set(), []
            for e, _ in grupo:
                for d in sel_por[e]:
                    if d["protocolo"] not in vistos:
                        vistos.add(d["protocolo"]); sel.append(d)
            sel.sort(key=lambda d: d["data"])
            k = cnpj + "_" + "-".join(e[1] for e, _ in grupo)
            ass = hashlib.sha1(json.dumps([d["protocolo"] + "_" + str(d["versao"]) for d in sel]).encode()).hexdigest()[:12]
            if not a.forcar and all(idx["emissoes"].get(chave_emissao(e), {}).get("assinatura") == ass and
                                    idx["emissoes"][chave_emissao(e)].get("status") == "ok" for e, _ in grupo):
                continue
            docs_txt = [(d, txt[d["protocolo"]]) for d in sel if txt.get(d["protocolo"])]
            if sum(len(t) for _, t in docs_txt) < 500:
                for e, itens in grupo:
                    idx["emissoes"][chave_emissao(e)] = {"codigos": [i["c"] for i in itens], "status": "texto_vazio"}
                continue
            prompt = montar_prompt(grupo, docs_txt)
            pendentes.append((k, grupo, sel, ass, prompt))
            log(f"  {k} {[i['c'] for _, its in grupo for i in its]}: {len(sel)} docs, {len(prompt):,} caracteres")

    modo = "lote" if a.lote else "direto"
    est = sum(len(p[4]) / 2.6 for p in pendentes)
    est_usd = (est * PRECO[modo][0] + len(pendentes) * 7000 * PRECO[modo][1]) / 1e6
    log(f"pendentes: {len(pendentes)} | ~{est / 1e6:.2f} MTok de entrada | custo estimado US$ {est_usd:.2f} | já gasto US$ {idx['gasto_usd']:.2f}")
    if idx["gasto_usd"] + est_usd > a.limite_usd:
        sys.exit(f"ABORTADO: estimativa passaria do teto de US$ {a.limite_usd}")
    if not pendentes:
        json.dump(idx, open(idx_p, "w"), ensure_ascii=False, indent=1)
        return

    resultados = {}
    if modo == "direto":
        for k, grupo, sel, ass, prompt in pendentes:
            r = json.loads(api("/v1/messages", params(prompt)))
            resultados[k] = ("".join(b.get("text", "") for b in r["content"]), r.get("usage", {}))
    else:
        req = [{"custom_id": p[0], "params": params(p[4])} for p in pendentes]
        b = json.loads(api("/v1/messages/batches", {"requests": req}))
        idx["lote"] = {"id": b["id"], "itens": {k: {"grupo": [[e[0], e[1], [i["c"] for i in its], its[0]["e"]] for e, its in g],
                                                     "sel": sel, "ass": ass} for k, g, sel, ass, _ in pendentes}}
        json.dump(idx, open(idx_p, "w"), ensure_ascii=False, indent=1)
        log("lote enviado:", b["id"])
        if not coletar_lote(idx, idx_p):
            sys.exit("lote ainda em processamento; a próxima execução coleta")
        idx["gasto_usd"] = round(idx["gasto_usd"] + classificar(), 4)
        json.dump(idx, open(idx_p, "w"), ensure_ascii=False, indent=1)
        log(f"gasto acumulado: US$ {idx['gasto_usd']:.2f}")
        return

    processar(idx, [(k, g, sel, ass) for k, g, sel, ass, _ in pendentes], resultados, modo)
    idx["gasto_usd"] = round(idx["gasto_usd"] + classificar(), 4)
    json.dump(idx, open(idx_p, "w"), ensure_ascii=False, indent=1)
    log(f"gasto acumulado: US$ {idx['gasto_usd']:.2f}")


if __name__ == "__main__":
    main()
