#!/usr/bin/env python3
"""Coletor diário do Painel de Dívidas (debêntures, CRI, CRA e afins).

Fontes
------
1. B3, Boletim Diário do Mercado, tabela "Negociação consolidada" (Renda fixa / Balcão)
   POST https://arquivos.b3.com.br/bdi/table/ConsolidatedRecords/<d>/<d>/<pag>/<tam>  corpo {}
   Janela disponível: ~últimos 21 dias úteis.
2. ANBIMA, Mercado Secundário de Debêntures (taxas e PU indicativos)
   GET https://www.anbima.com.br/informacoes/merc-sec-debentures/arqs/dbAAMMDD.txt

Saída (em docs/data, servida pelo GitHub Pages)
-----
  daily/AAAA-MM-DD.json   foto do dia, um registro por ativo
  series/<CODIGO>.json    histórico compacto por ativo (para o gráfico)
  index.json              datas disponíveis + data mais recente
  _debug/                 colunas cruas recebidas (para ajustar o mapeamento)

Só biblioteca padrão: nada para instalar, um modo de falha a menos.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
DADOS = RAIZ / "docs" / "data"
BRT = timezone(timedelta(hours=-3))
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

B3_URL = "https://arquivos.b3.com.br/bdi/table/{tabela}/{d}/{d}/{pag}/{tam}"
ANBIMA_URL = "https://www.anbima.com.br/informacoes/merc-sec-debentures/arqs/db{d}.txt"

# Limites dos alertas (ajustáveis)
ALERTA_PCT_PAR = 80.0        # PU abaixo de X% do par
ALERTA_DESVIO_ANBIMA = 2.0   # |PU negociado / PU ANBIMA - 1| acima de X%
ALERTA_VOLUME_MULT = 3.0     # volume do dia > X vezes a média dos últimos 20 pregões
ALERTA_VOLUME_MIN = 1_000_000.0
SERIE_MAX_PONTOS = 750       # ~3 anos de pregões por ativo
# Escopo: dívida corporativa e do agro. Fora: papéis bancários (CDB, LCA, LCI, LF, CCB, COE...)
# e cotas de fundos (CFF), que somam a maior parte das linhas da B3.
TIPOS_INCLUIR = {"DEB", "CRI", "CRA", "NC", "CDCA", "CPR"}
B3_TAM_PAG = 500
B3_TIMEOUT = 15     # a API da B3 responde em <2s ou trava: desistir rápido e tentar de novo
B3_TENTATIVAS = 8


# ----------------------------------------------------------------------------- utilidades

def log(msg: str) -> None:
    print(f"[{datetime.now(BRT):%H:%M:%S}] {msg}", flush=True)


def norm(s: str) -> str:
    """'Preço Médio' -> 'precomedio' (sem acento, minúsculo, só alfanumérico)."""
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


def num(v):
    """Converte número em formato BR ('1.234,56'), US ou nativo. Vazio/traço -> None."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("%", "").replace("R$", "").strip()
    if s in ("", "-", "--", "N/D", "ND", "n/d", "*", "null"):
        return None
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def http(url: str, metodo: str = "GET", corpo: bytes | None = None,
         tentativas: int = 3, timeout: int = 60) -> bytes | None:
    hdr = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "pt-BR,pt;q=0.9"}
    if corpo is not None:
        hdr["Content-Type"] = "application/json"
    for i in range(1, tentativas + 1):
        try:
            req = urllib.request.Request(url, data=corpo, headers=hdr, method=metodo)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                log(f"  404 em {url}")
                return None
            log(f"  HTTP {e.code} em {url} (tentativa {i})")
        except Exception as e:  # rede, timeout, TLS
            log(f"  erro {type(e).__name__}: {e} em {url} (tentativa {i})")
        time.sleep(min(2 * i, 8))
    return None


def dia_util_anterior(d: date) -> date:
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def escrever_json(caminho: Path, obj) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")),
                       encoding="utf-8")


# ----------------------------------------------------------------------------- B3

# nome lógico -> candidatos (normalizados). Cobre rótulo PT, EN e tag ISO usada na API.
B3_MAPA = {
    "tipo":      ["instrumentcode", "instrumentofinanceiro", "instrumento", "financialinstrument", "instrmtp",
                  "instrmnttp", "tipoinstrumento", "tipo", "sctyctgynm", "sctyctgy"],
    "codigo":    ["codigoif", "codif", "tickersymbol", "tckrsymb", "codigo", "ifcode"],
    "isin":      ["codigoisin", "isin", "isincode"],
    "emissor":   ["issuer", "emissor", "issrnm", "issuername", "nomeemissor", "emissornm"],
    "data_neg":  ["tradedate", "datanegocio", "trddt", "traddt", "rptdt"],
    "data_liq":  ["settlementdt", "dataliquidacao", "settlementdate", "sttlmdt"],
    "qtd":       ["quantity", "quantidadenegociada", "tradedquantity", "tradqty", "trdqty", "quantidade"],
    "pu_min":    ["minimum", "precominimo", "minprice", "minpric", "minprc"],
    "pu_med":    ["average", "precomedio", "averageprice", "avrgpric", "avgpric", "avgprc"],
    "pu_max":    ["maximum", "precomaximo", "maxprice", "maxpric", "maxprc"],
    "pu_ult":    ["closing", "ultimopreco", "lastprice", "lastpric", "lastprc"],
    "pu_ref":    ["referenceprice", "precodereferencia", "precoreferencia", "referenceprice", "refpric", "mtmpric"],
    "n_neg":     ["numerodenegocios", "numberoftrades", "nbroftrds", "nboftrds", "tradsqty", "qtdnegocios",
                  "rglrtxsqty"],
    "volume":    ["volumefinanceiror", "volumefinanceiro", "financialvolume", "ntlfinvol",
                  "fnclvol", "volume"],
    "classif":   ["businessclass", "classificacaodonegocio", "classificacaodosnegocios", "classificacao",
                  "tradeclassification", "trdclssfctn"],
    "oscilacao": ["osc", "oscilacao", "oscillation", "oscn", "prcvartn"],
}


def mapear_colunas(colunas: list[str]) -> dict[str, int]:
    ncols = [norm(c) for c in colunas]
    idx: dict[str, int] = {}
    for campo, candidatos in B3_MAPA.items():
        for cand in candidatos:  # match exato primeiro
            if cand in ncols and ncols.index(cand) not in idx.values():
                idx[campo] = ncols.index(cand)
                break
        else:
            for cand in candidatos:  # depois "contém"
                hit = next((i for i, c in enumerate(ncols)
                            if cand in c and i not in idx.values()), None)
                if hit is not None:
                    idx[campo] = hit
                    break
    return idx


def b3_pagina(tabela: str, d: date, pag: int):
    raw = http(B3_URL.format(tabela=tabela, d=d.isoformat(), pag=pag, tam=B3_TAM_PAG),
               metodo="POST", corpo=b"{}", tentativas=B3_TENTATIVAS, timeout=B3_TIMEOUT)
    if raw is None:
        return None
    try:
        return json.loads(raw).get("table") or {}
    except json.JSONDecodeError:
        return None


def b3_tabela(tabela: str, d: date) -> tuple[list[str], list[list]] | None:
    """Lê todas as páginas (6 em paralelo). Devolve None se alguma página falhar,
    para a data não ser gravada pela metade e ser refeita na próxima execução."""
    colunas: list[str] = []
    linhas: list[list] = []
    hashes = set()
    pag = 1
    with ThreadPoolExecutor(8) as ex:
        while pag < 600:
            lote = list(range(pag, pag + 8))
            res = list(ex.map(lambda p: b3_pagina(tabela, d, p), lote))
            fim = False
            for p, t in zip(lote, res):
                if t is None:
                    log(f"  B3: página {p} falhou após {B3_TENTATIVAS} tentativas; data fica para depois")
                    return None
                if not colunas and t.get("columns"):
                    colunas = [c.get("name") or f"c{i}" for i, c in enumerate(t["columns"])]
                    escrever_json(DADOS / "_debug" / f"b3_{tabela}_colunas.json",
                                  {"data": d.isoformat(), "columns": t["columns"],
                                   "amostra": (t.get("values") or [])[:3]})
                vals = t.get("values") or []
                if not vals:
                    fim = True
                    break
                h = hashlib.sha1(json.dumps(vals, sort_keys=True).encode()).hexdigest()
                if h in hashes:  # endpoint repetindo página
                    fim = True
                    break
                hashes.add(h)
                linhas.extend(vals)
                if len(vals) < B3_TAM_PAG:
                    fim = True
                    break
            if fim:
                break
            pag += 8
    return colunas, linhas


def coletar_b3(d: date) -> dict[str, dict]:
    r = b3_tabela("ConsolidatedRecords", d)
    if r is None:
        return None
    colunas, linhas = r
    if not linhas:
        return {}
    idx = mapear_colunas(colunas)
    faltando = [k for k in ("codigo", "qtd", "pu_med") if k not in idx]
    if faltando:
        log(f"  ATENÇÃO: colunas B3 não mapeadas {faltando}. Recebidas: {colunas}")
        if "codigo" in faltando:
            return {}

    def g(row, campo):
        i = idx.get(campo)
        return row[i] if i is not None and i < len(row) else None

    ativos: dict[str, dict] = {}
    por_tipo: dict[str, list] = {}
    for row in linhas:
        cod = str(g(row, "codigo") or "").strip().upper()
        if not cod or cod == "-":
            continue
        tp = str(g(row, "tipo") or "").strip().upper()
        pt = por_tipo.setdefault(tp, [0, 0.0])
        pt[0] += 1
        pt[1] += num(g(row, "volume")) or 0
        if tp not in TIPOS_INCLUIR:
            continue
        a = ativos.setdefault(cod, {
            "tipo": (str(g(row, "tipo") or "").strip().upper() or None),
            "isin": (str(g(row, "isin") or "").strip() or None),
            "emissor": (str(g(row, "emissor") or "").strip() or None),
            "qtd": 0.0, "volume": 0.0, "n_neg": 0, "pu_min": None, "pu_max": None,
            "pu_ult": None, "pu_ref": None, "oscilacao": None,
            "vol_extragrupo": 0.0, "vol_intragrupo": 0.0,
        })
        qtd = num(g(row, "qtd")) or 0.0
        pmed = num(g(row, "pu_med"))
        vol = num(g(row, "volume"))
        if vol is None and pmed is not None:
            vol = qtd * pmed
        vol = vol or 0.0
        a["qtd"] += qtd
        a["volume"] += vol
        a["n_neg"] += int(num(g(row, "n_neg")) or 0)
        for campo, fn in (("pu_min", min), ("pu_max", max)):
            v = num(g(row, campo))
            if v is not None:
                a[campo] = v if a[campo] is None else fn(a[campo], v)
        for campo in ("pu_ult", "pu_ref", "oscilacao"):
            v = num(g(row, campo))
            if v is not None:
                a[campo] = v
        cl = norm(g(row, "classif") or "")
        if "extra" in cl:
            a["vol_extragrupo"] += vol
        elif "intra" in cl:
            a["vol_intragrupo"] += vol
    for a in ativos.values():
        a["pu_med"] = round(a["volume"] / a["qtd"], 6) if a["qtd"] else None
        a["volume"] = round(a["volume"], 2)
    resumo = ", ".join(f"{k}={v[0]} (R$ {v[1]/1e6:,.0f} mi)" for k, v in
                       sorted(por_tipo.items(), key=lambda x: -x[1][1]))
    log(f"  B3: {len(linhas)} linhas; por tipo: {resumo}")
    log(f"  B3: {len(ativos)} ativos no escopo (tipos: {sorted(TIPOS_INCLUIR)})")
    return ativos


# ----------------------------------------------------------------------------- ANBIMA

ANBIMA_MAPA = {
    "codigo": ["codigo"], "nome": ["nome"], "vencimento": ["repacvenc", "venc"],
    "indice": ["indicecorrecao", "indice"], "tx_compra": ["taxadecompra"],
    "tx_venda": ["taxadevenda"], "tx_ind": ["taxaindicativa"], "desvio": ["desviopadrao"],
    "int_min": ["intervaloindicativominimo", "intervalominimo"],
    "int_max": ["intervaloindicativomaximo", "intervalomaximo"],
    "pu": ["pu"], "pct_par": ["pupar"], "duration": ["duration"], "pct_reune": ["reune"],
    "ref_ntnb": ["referenciantnb", "ntnb"],
}
COD_RE = re.compile(r"^[A-Z0-9]{4,8}\d{0,2}$")


def parse_anbima(texto: str) -> dict[str, dict]:
    linhas = texto.splitlines()
    sep = None
    cab_idx = None
    for i, l in enumerate(linhas):
        if norm(l).startswith("codigo"):
            sep = max("@;\t|", key=l.count)
            cab_idx = i
            break
    if sep is None:
        return {}
    cab = [norm(c) for c in linhas[cab_idx].split(sep)]
    pos: dict[str, int] = {}
    for campo, cands in ANBIMA_MAPA.items():
        for c in cands:
            hit = next((i for i, h in enumerate(cab) if (h == c or (len(c) > 3 and c in h))
                        and i not in pos.values()), None)
            if hit is not None:
                pos[campo] = hit
                break
    out: dict[str, dict] = {}
    categoria = None
    for l in linhas[cab_idx + 1:]:
        if sep not in l:
            t = l.strip().upper()
            if t and re.fullmatch(r"[A-Z_\- ]{3,40}", t):
                categoria = t.replace(" ", "_")
            continue
        p = [x.strip() for x in l.split(sep)]
        if norm(p[0]).startswith("codigo"):
            continue
        cod = p[pos.get("codigo", 0)].upper()
        if not COD_RE.match(cod):
            continue

        def g(c):
            i = pos.get(c)
            return p[i] if i is not None and i < len(p) else None

        r = {
            "nome": g("nome"), "vencimento": g("vencimento"), "indice": g("indice"),
            "categoria": categoria,
        }
        for c in ("tx_compra", "tx_venda", "tx_ind", "desvio", "int_min", "int_max",
                  "pu", "pct_par", "duration", "pct_reune"):
            r[c] = num(g(c))
        if r["pu"] and r["pct_par"]:
            r["pu_par"] = round(r["pu"] / (r["pct_par"] / 100), 6)
        out[cod] = r
    return out


def coletar_anbima(d: date) -> dict[str, dict]:
    raw = http(ANBIMA_URL.format(d=d.strftime("%y%m%d")))
    if not raw:
        return {}
    txt = raw.decode("latin-1", errors="replace")
    if "<html" in txt[:500].lower():
        log("  ANBIMA devolveu HTML em vez do .txt (arquivo ainda não publicado?)")
        return {}
    (DADOS / "_debug").mkdir(parents=True, exist_ok=True)
    (DADOS / "_debug" / "anbima_amostra.txt").write_text(
        "\n".join(txt.splitlines()[:15]), encoding="utf-8")
    r = parse_anbima(txt)
    log(f"  ANBIMA: {len(r)} debêntures com preço indicativo")
    return r


# ----------------------------------------------------------------------------- SND (debentures.com.br)

SND_BASE = "https://www.debentures.com.br/exploreosnd/consultaadados/emissoesdedebentures/"


def _snd_tsv(url: str, marcador: str) -> tuple[list[str], list[list[str]]]:
    raw = http(url, tentativas=3, timeout=120)
    if not raw:
        return [], []
    linhas = raw.decode("cp1252", errors="replace").replace("\r", "").split("\n")
    i = next((k for k, l in enumerate(linhas[:30]) if l.startswith(marcador)), None)
    if i is None:
        log(f"  SND: cabeçalho '{marcador}' não encontrado em {url}")
        return [], []
    cab = [c.strip() for c in linhas[i].split("\t")]
    rows = [[c.strip() for c in l.split("\t")] for l in linhas[i + 1:] if l.count("\t") > 3]
    return cab, rows


def coletar_snd_cadastro() -> dict[str, dict]:
    """Características de todas as debêntures registradas no SND (vencimento, remuneração...)."""
    cab, rows = _snd_tsv(SND_BASE + "caracteristicas_e.asp?tip_deb=publicas&selecao=", "Codigo do Ativo")
    if not rows:
        return {}
    ix = {norm(c): k for k, c in enumerate(cab)}

    def g(r, nome):
        k = ix.get(norm(nome))
        return r[k] if k is not None and k < len(r) else ""

    out = {}
    for r in rows:
        cod = g(r, "Codigo do Ativo").upper()
        if not cod or g(r, "Situacao") != "Registrado":
            continue
        qm = num(g(r, "Quantidade em Mercado")) or 0
        venc = g(r, "Data de Saida / Novo Vencimento") or g(r, "Data de Vencimento")
        out[cod] = {
            "empresa": g(r, "Empresa"), "serie": g(r, "Serie"), "emissao": g(r, "Emissao").lstrip("0"),
            "isin": g(r, "ISIN"), "dt_emissao": g(r, "Data de Emissao"), "venc": venc,
            "garantia": g(r, "Garantia/Especie"), "qtd_emitida": num(g(r, "Quantidade Emitida")),
            "qtd_mercado": qm, "vne": num(g(r, "Valor Nominal na Emissao")),
            "indice": g(r, "indice"), "mult": num(g(r, "Percentual Multiplicador/Rentabilidade")),
            "taxa": num(g(r, "Juros Criterio Novo - Taxa")),
            "amort": g(r, "Tipo de Amortizacao") not in ("", "-"),
            "incentivada": g(r, "Deb. Incent. (Lei 12.431)") == "S",
            "resgate_antecipado": g(r, "Resgate Antecipado") == "S",
            "cnpj": g(r, "CNPJ"), "fiduciario": g(r, "Agente Fiduciario"),
        }
    log(f"  SND cadastro: {len(out)} debêntures registradas")
    return out


def coletar_snd_pu(d: date) -> dict[str, dict]:
    """PU de curva (valor nominal atualizado + juros) de todas as debêntures na data."""
    ds = urllib.parse.quote(d.strftime("%d/%m/%Y"))
    cab, rows = _snd_tsv(SND_BASE + f"puhistorico_e.asp?op_exc=False&ativo=&dt_ini={ds}&dt_fim={ds}",
                         "Data do PU")
    out = {}
    for r in rows:
        if len(r) < 6:
            continue
        pu = num(r[5])
        if pu:
            out[r[1].upper()] = {"vna": num(r[2]), "juros": num(r[3]), "pu_par": pu}
    log(f"  SND PU de curva: {len(out)} debêntures")
    return out


# ----------------------------------------------------------------------------- ANBIMA CRI/CRA

ANBIMA_CRICRA_URL = ("https://www.anbima.com.br/pt_br/anbima/TaxasCriCraExport/exportarCSV"
                     "?filtroTermo=&filtroData={d}")


def coletar_anbima_cricra(d: date) -> dict[str, dict]:
    raw = http(ANBIMA_CRICRA_URL.format(d=urllib.parse.quote(d.strftime("%d/%m/%Y"))),
               tentativas=3, timeout=60)
    if not raw or len(raw) < 200:
        return {}
    try:
        txt = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        txt = raw.decode("latin-1")
    ls = [l for l in txt.replace("\r", "").split("\n") if l.strip()]
    i = next((k for k, l in enumerate(ls[:5]) if "digo" in l and ";" in l), None)
    if i is None:
        return {}
    cab = [norm(c) for c in ls[i].split(";")]

    def col(*nomes):
        for n in nomes:
            for k, c in enumerate(cab):
                if c == n or (len(n) > 4 and n in c):
                    return k
        return None

    k = {"cod": col("codigo"), "dev": col("riscodecredito"), "sec": col("emissor"),
         "serie": col("serie"), "emissao": col("emissao"), "venc": col("vencimento"),
         "idx": col("indicecorrecao"), "txc": col("taxacompra"), "txv": col("taxavenda"),
         "tx": col("taxaindicativa"), "pu": col("pu"), "pct": col("pupar"), "dur": col("duration")}
    out = {}
    for l in ls[i + 1:]:
        p = [x.strip() for x in l.split(";")]

        def g(n):
            j = k.get(n)
            return p[j] if j is not None and j < len(p) else ""

        cod = g("cod").upper()
        if not cod:
            continue
        pu, pct = num(g("pu")), num(g("pct"))
        out[cod] = {
            "devedor": g("dev"), "securitizadora": g("sec"), "serie": g("serie"),
            "emissao": g("emissao"), "vencimento": g("venc"), "indice": g("idx"),
            "tx_compra": num(g("txc")), "tx_venda": num(g("txv")), "tx_ind": num(g("tx")),
            "pu": pu, "pct_par": pct, "duration": num(g("dur")),
            "pu_par": round(pu / (pct / 100), 6) if pu and pct else None,
        }
    log(f"  ANBIMA CRI/CRA: {len(out)} certificados")
    return out


# ----------------------------------------------------------------------------- yield implícito

def base_indexador(txt: str | None, mult: float | None = None) -> str | None:
    """'DI+' (spread), 'IPCA+', 'IGPM+', 'PRE', '%DI' a partir do texto do indexador."""
    t = norm(txt or "")
    if not t:
        return None
    if "doDI".lower() in t or (t.startswith("di") and mult not in (None, 100.0)):
        return "%DI"
    if t.startswith("di") or "cdi" in t:
        return "DI+"
    if "ipca" in t:
        return "IPCA+"
    if "igp" in t:
        return "IGPM+"
    if "pre" in t:
        return "PRE"
    return None


def taxa_do_texto(txt: str | None) -> float | None:
    m = re.search(r"\+\s*([\d.,]+)\s*%", txt or "")
    return num(m.group(1)) if m else None


def du_ate(venc: str | None, d: date) -> int | None:
    try:
        v = datetime.strptime(venc, "%d/%m/%Y").date()
    except (TypeError, ValueError):
        return None
    dias = (v - d).days
    return max(int(dias * 252 / 365), 1) if dias > 0 else None


def duration_estimada(du: int, taxa_total: float) -> float:
    """Macaulay de um título com cupom = taxa, sem amortização (aproximação), em dias úteis."""
    T, y = du / 252, max(taxa_total, 0.01)
    return ((1 + y) / y * (1 - (1 + y) ** -T)) * 252


def yield_implicito(pu_neg, pu_ref, y_ref_pct, dur_du):
    """(1+y) = (1+y_ref) * (PU_ref/PU_neg)^(252/duration)."""
    if not (pu_neg and pu_ref and dur_du and y_ref_pct is not None) or dur_du < 5:
        return None
    try:
        return round(((1 + y_ref_pct / 100) * (pu_ref / pu_neg) ** (252 / dur_du) - 1) * 100, 2)
    except (OverflowError, ZeroDivisionError):
        return None


CDI_APROX = 0.14  # só para estimar duration de papéis DI+ sem duration ANBIMA


def calcular_preco_e_yield(t: str | None, d: date, neg: dict | None, anb: dict | None,
                           snd: dict | None, curva: dict | None) -> dict:
    """% do par e yield implícito (HTM) do PU negociado."""
    pmed = (neg or {}).get("pu_med")
    # ANBIMA primeiro (calcula o par com VNA atualizado); SND como alternativa, pois em
    # alguns papéis o PU do SND vem igual ao valor de emissão, sem atualização
    pu_par = (anb or {}).get("pu_par") or (curva or {}).get("pu_par")
    fonte_par = "ANBIMA" if (anb or {}).get("pu_par") else ("SND" if (curva or {}).get("pu_par") else None)
    out = {"pu_par": pu_par, "fonte_par": fonte_par, "pct_par_neg": None, "y": None,
           "y_base": None, "y_est": False}
    if pmed and pu_par:
        out["pct_par_neg"] = round(pmed / pu_par * 100, 2)
    if not pmed:
        return out
    pct = out["pct_par_neg"]
    if pct is not None and not (70 <= pct <= 130):
        return out  # fora dessa faixa o yield não diz nada (recuperação, não carrego)
    # 1) referência ANBIMA: taxa indicativa + PU + duration
    if anb and anb.get("tx_ind") is not None and anb.get("pu") and anb.get("duration"):
        base = base_indexador(anb.get("indice"))
        if base and base != "%DI":
            y = yield_implicito(pmed, anb["pu"], anb["tx_ind"], anb["duration"])
            if y is not None and -5 <= y <= 40:
                out.update(y=y, y_base=base)
            return out
    # 2) referência contratual: no par, o papel rende o próprio cupom (estimativa)
    if snd and pu_par and pct is not None and 90 <= pct <= 110:
        base = base_indexador(snd.get("indice"), snd.get("mult"))
        if base and base != "%DI" and snd.get("taxa") is not None:
            du = du_ate(snd.get("venc"), d)
            if du:
                total = (snd["taxa"] / 100) + (CDI_APROX if base == "DI+" else 0.05 if base != "PRE" else 0)
                dur = duration_estimada(du, total) * (0.6 if snd.get("amort") else 1.0)
                y = yield_implicito(pmed, pu_par, snd["taxa"], dur)
                if y is not None and -1 <= y <= 25 and du > 120:
                    out.update(y=y, y_base=base, y_est=True)
    return out


# ----------------------------------------------------------------------------- séries e alertas

def carregar_serie(cod: str) -> list[list]:
    f = DADOS / "series" / f"{cod}.json"
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))["p"]
        except Exception:
            return []
    return []


# ponto: [data, pu_med, qtd, volume, n_neg, pu_anbima, tx_ind, pct_par_anbima, pu_par, pct_par_neg, y]
def atualizar_serie(cod: str, d: str, neg: dict | None, anb: dict | None, py: dict) -> list[list]:
    pts = [p for p in carregar_serie(cod) if p[0] != d]
    pts.append([
        d,
        neg.get("pu_med") if neg else None,
        neg.get("qtd") if neg else None,
        neg.get("volume") if neg else None,
        neg.get("n_neg") if neg else None,
        anb.get("pu") if anb else None,
        anb.get("tx_ind") if anb else None,
        anb.get("pct_par") if anb else None,
        py.get("pu_par"), py.get("pct_par_neg"), py.get("y"),
    ])
    pts.sort(key=lambda p: p[0])
    pts = pts[-SERIE_MAX_PONTOS:]
    escrever_json(DADOS / "series" / f"{cod}.json", {"c": cod, "p": pts})
    return pts


def alertas(neg: dict | None, anb: dict | None, py: dict, serie: list[list], ds: str) -> list[str]:
    out = []
    if anb and anb.get("categoria") and "VENCID" in anb["categoria"]:
        out.append("vencida_antecipadamente")
    if py.get("pct_par_neg") is not None and py["pct_par_neg"] < ALERTA_PCT_PAR:
        out.append("negociado_abaixo_par")
    elif anb and anb.get("pct_par") is not None and anb["pct_par"] < ALERTA_PCT_PAR:
        out.append("indicativo_abaixo_par")
    pmed = neg.get("pu_med") if neg else None
    if pmed and anb and anb.get("pu"):
        if abs(pmed / anb["pu"] - 1) * 100 > ALERTA_DESVIO_ANBIMA:
            out.append("desvio_anbima")
    if neg and neg.get("volume"):
        janela = [q for q in serie if q[0] < ds][-20:]
        media = sum(q[3] or 0 for q in janela) / len(janela) if janela else 0  # dia sem negócio = 0
        # só alerta com histórico mínimo, para não disparar em massa nos primeiros dias
        if (len(janela) >= 10 and neg["volume"] >= ALERTA_VOLUME_MIN
                and neg["volume"] > ALERTA_VOLUME_MULT * max(media, 1.0)):
            out.append("salto_volume")
    return out


# ----------------------------------------------------------------------------- empresas

SUFIXOS = re.compile(r"\b(S\.?\s?/?A\.?|LTDA\.?|CIA\.?|COMPANHIA|EM RECUPERACAO JUDICIAL|- EM RECUPERACAO JUDICIAL)\b")


def chave_empresa(nome: str | None) -> str:
    s = unicodedata.normalize("NFKD", (nome or "").upper()).encode("ascii", "ignore").decode()
    s = SUFIXOS.sub(" ", s)
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def empresa_do_ativo(t, cod, neg, anb_cc, snd, anb_deb) -> tuple[str | None, str | None]:
    """(empresa devedora, securitizadora). CRI/CRA: devedor vem da ANBIMA quando há."""
    if t in ("CRI", "CRA"):
        sec = (neg or {}).get("emissor") or (anb_cc or {}).get("securitizadora")
        dev = (anb_cc or {}).get("devedor")
        return (dev or sec), sec
    return ((snd or {}).get("empresa") or (neg or {}).get("emissor") or (anb_deb or {}).get("nome")), None


# ----------------------------------------------------------------------------- orquestração

_CACHE: dict = {}


def snd_cadastro() -> dict:
    if "snd" not in _CACHE:
        _CACHE["snd"] = coletar_snd_cadastro()
    return _CACHE["snd"]


def processar(d: date, forcar: bool = False) -> bool:
    ds = d.isoformat()
    destino = DADOS / "daily" / f"{ds}.json"
    log(f"Data {ds}")
    if destino.exists() and not forcar:
        antigo = json.loads(destino.read_text(encoding="utf-8"))
        f = antigo["fontes"]
        # completo = tem B3, SND e (ANBIMA ou a ANBIMA já não guarda mais essa data)
        idade = (datetime.now(BRT).date() - d).days
        # hoje e ontem sempre são refeitos: a B3 consolida o pregão só na manhã seguinte
        if idade >= 2 and f.get("b3") and f.get("snd_pu") and (f.get("anbima") or idade > 4):
            log("  já completo; pulando")
            return False
    neg = coletar_b3(d)
    if neg is None:
        return False
    anb = coletar_anbima(d)
    anb_cc = coletar_anbima_cricra(d)
    curva = coletar_snd_pu(d)
    snd = snd_cadastro()
    if not neg and not anb and not anb_cc:
        log("  sem dados (feriado, fim de semana ou ainda não publicado)")
        return False
    if destino.exists() and not forcar:
        f = json.loads(destino.read_text(encoding="utf-8"))["fontes"]
        if (not neg and f.get("b3")) or (not anb and f.get("anbima")):
            log("  uma fonte veio vazia e o arquivo existente é mais completo; mantendo")
            return False

    ativos = []
    for cod in sorted(set(neg) | set(anb) | set(anb_cc)):
        n, a_deb, a_cc = neg.get(cod), anb.get(cod), anb_cc.get(cod)
        t = (n or {}).get("tipo") or ("DEB" if a_deb else ("CRA" if cod.startswith("CRA") else "CRI") if a_cc else None)
        a = a_deb or a_cc  # referência ANBIMA (mesmos campos-chave)
        s = snd.get(cod) if t == "DEB" else None
        cv = curva.get(cod) if t == "DEB" else None
        py = calcular_preco_e_yield(t, d, n, a, s, cv)
        serie = atualizar_serie(cod, ds, n, a, py)
        emp, sec = empresa_do_ativo(t, cod, n, a_cc, s, a_deb)
        reg = {
            "c": cod, "t": t, "e": emp, "s": sec,
            "i": (n or {}).get("isin") or (s or {}).get("isin"),
            "n": {k: n[k] for k in ("qtd", "volume", "n_neg", "pu_min", "pu_med", "pu_max",
                                    "pu_ult", "pu_ref", "oscilacao", "vol_extragrupo",
                                    "vol_intragrupo")} if n else None,
            "a": {k: a.get(k) for k in ("vencimento", "indice", "tx_compra", "tx_venda",
                                        "tx_ind", "int_min", "int_max", "pu", "pct_par",
                                        "pu_par", "duration", "categoria")} if a else None,
            "p": py,
        }
        reg["al"] = alertas(n, a, py, serie, ds)
        ativos.append(reg)

    escrever_json(destino, {
        "data": ds,
        "gerado_em": datetime.now(BRT).isoformat(timespec="seconds"),
        "fontes": {"b3": len(neg), "anbima": len(anb), "anbima_cricra": len(anb_cc),
                   "snd_pu": len(curva)},
        "ativos": ativos,
    })
    atualizar_indice()
    log(f"  gravado {destino.relative_to(RAIZ)} ({len(ativos)} ativos)")
    return True


def atualizar_indice() -> None:
    datas = sorted((p.stem for p in (DADOS / "daily").glob("*.json")), reverse=True)
    escrever_json(DADOS / "index.json", {
        "ultima": datas[0] if datas else None,
        "datas": datas,
        "atualizado_em": datetime.now(BRT).isoformat(timespec="seconds"),
    })


def construir_catalogo() -> None:
    """Universo por empresa: todas as debêntures em mercado (SND) + CRI/CRA vistos,
    com a foto de mercado mais recente de cada título."""
    datas = sorted((p.stem for p in (DADOS / "daily").glob("*.json")))
    if not datas:
        return
    ult = json.loads((DADOS / "daily" / f"{datas[-1]}.json").read_text(encoding="utf-8"))
    hoje = {a["c"]: a for a in ult["ativos"]}
    snd = snd_cadastro()
    # último negócio de cada título nos últimos 30 pregões
    ultimo_neg: dict[str, tuple] = {}
    info: dict[str, dict] = {}
    for ds in datas[-30:]:
        for a in json.loads((DADOS / "daily" / f"{ds}.json").read_text(encoding="utf-8"))["ativos"]:
            info[a["c"]] = a
            if a.get("n"):
                ultimo_neg[a["c"]] = (ds, a["n"].get("pu_med"), a["n"].get("volume"),
                                      (a.get("p") or {}).get("pct_par_neg"), (a.get("p") or {}).get("y"),
                                      (a.get("p") or {}).get("y_base"), (a.get("p") or {}).get("y_est"))
    cods = {c for c, s in snd.items() if (s.get("qtd_mercado") or 0) > 0} | set(info)
    itens = []
    for c in sorted(cods):
        s, a = snd.get(c), info.get(c) or {}
        h = hoje.get(c) or {}
        t = a.get("t") or ("DEB" if s else None)
        if t not in TIPOS_INCLUIR:
            continue
        anb = (h.get("a") or a.get("a")) or {}
        un = ultimo_neg.get(c)
        emp = a.get("e") or (s or {}).get("empresa")
        idx_txt = anb.get("indice")
        if not idx_txt and s:
            base = base_indexador(s.get("indice"), s.get("mult"))
            if base == "%DI":
                idx_txt = f"{f2(s.get('mult'))}% do DI"
            elif base == "PRE":
                idx_txt = f"{f2(s.get('taxa'))}% a.a."
            elif base:
                idx_txt = f"{base[:-1]} + {f2(s.get('taxa'))}%"
        itens.append({
            "c": c, "t": t, "e": emp, "k": chave_empresa(emp), "s": a.get("s"),
            "ven": anb.get("vencimento") or (s or {}).get("venc"),
            "idx": idx_txt, "gar": (s or {}).get("garantia"), "inc": (s or {}).get("incentivada"),
            "em": (s or {}).get("emissao"), "se": (s or {}).get("serie"),
            "qm": (s or {}).get("qtd_mercado"), "vne": (s or {}).get("vne"),
            "cnpj": (s or {}).get("cnpj"), "ra": (s or {}).get("resgate_antecipado"),
            "par": (h.get("p") or {}).get("pu_par"),
            "ud": un[0] if un else None, "up": un[1] if un else None, "uv": un[2] if un else None,
            "upp": un[3] if un else None, "uy": un[4] if un else None,
            "uyb": un[5] if un else None, "uye": un[6] if un else None,
            "vh": (h.get("n") or {}).get("volume"),
            "ta": anb.get("tx_ind"), "pa": anb.get("pu"), "ppa": anb.get("pct_par"),
            "du": anb.get("duration"), "cat": anb.get("categoria"), "al": h.get("al") or [],
        })
    escrever_json(DADOS / "catalogo.json", {"data": datas[-1], "itens": itens})
    log(f"Catálogo: {len(itens)} títulos, {len({i['k'] for i in itens})} empresas")


def f2(v) -> str:
    return "" if v is None else f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", help="AAAA-MM-DD (padrão: hoje em Brasília)")
    ap.add_argument("--backfill", type=int, default=0,
                    help="também processa os N dias úteis anteriores")
    ap.add_argument("--forcar", action="store_true", help="sobrescreve o arquivo do dia")
    args = ap.parse_args()

    d = date.fromisoformat(args.data) if args.data else datetime.now(BRT).date()
    datas = []
    if d.weekday() < 5:
        datas.append(d)
    x = d
    for _ in range(max(args.backfill, 1)):
        x = dia_util_anterior(x)
        datas.append(x)
    # do mais antigo para o mais novo, para os alertas de volume enxergarem o histórico
    ok = [processar(dd, args.forcar) for dd in sorted(datas)]
    log(f"Fim: {sum(ok)}/{len(ok)} datas gravadas")
    try:
        construir_catalogo()
    except Exception as e:  # catálogo nunca derruba a coleta
        log(f"  erro ao montar catálogo: {type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
