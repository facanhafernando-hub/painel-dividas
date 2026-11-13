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
import collections
import math
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
        if venc[-4:].isdigit() and int(venc[-4:]) >= 2090:   # placeholder do SND
            venc = g(r, "Data de Vencimento") if g(r, "Data de Vencimento")[-4:] < "2090" else venc
        out[cod] = {
            "empresa": g(r, "Empresa"), "serie": g(r, "Serie"), "emissao": g(r, "Emissao").lstrip("0"),
            "isin": g(r, "ISIN"), "dt_emissao": g(r, "Data de Emissao"), "venc": venc,
            "garantia": g(r, "Garantia/Especie"), "qtd_emitida": num(g(r, "Quantidade Emitida")),
            "qtd_mercado": qm, "vne": num(g(r, "Valor Nominal na Emissao")),
            "vn_atual": num(g(r, "Valor Nominal Atual")),
            "indice": g(r, "indice"), "mult": num(g(r, "Percentual Multiplicador/Rentabilidade")),
            "taxa": num(g(r, "Juros Criterio Novo - Taxa")),
            "amort": g(r, "Tipo de Amortizacao") not in ("", "-"),
            "amort_taxa": num(g(r, "Amortizacao - Taxa")), "amort_cada": g(r, "Amortizacao - Cada"),
            "amort_un": g(r, "Amortizacao - Unidade"), "amort_car": g(r, "Amortizacao - Carencia"),
            "juros_cada": g(r, "Juros Criterio Novo - Cada"), "juros_un": g(r, "Juros Criterio Novo - Unidade"),
            "juros_car": g(r, "Juros Criterio Novo - Carencia"),
            "inicio_rent": g(r, "Data do Inicio da Rentabilidade"),
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


# ----------------------------------------------------------------------------- SND: agenda de eventos (cronograma)

SND_AGENDA = ("https://www.debentures.com.br/exploreosnd/consultaadados/eventosfinanceiros/agenda_e.asp"
              "?op_exc=False&ativo=&dt_ini={di}&dt_fim={df}&evento=")


def coletar_snd_agenda(hoje: date) -> dict[str, list]:
    """Todos os eventos futuros de todas as debêntures. Evento: [data_pagamento, código, taxa/percentual, base]
    com código J (juros), A (amortização), I (incorporação de juros), P (prêmio), V (vencimento) e base
    FIX (% do valor de emissão) ou VAR (% do saldo)."""
    janelas = [(hoje + timedelta(days=365 * k), hoje + timedelta(days=365 * k + 364)) for k in range(8)]
    janelas.append((hoje + timedelta(days=365 * 8), date(2099, 12, 31)))
    out: dict[str, list] = {}
    q = urllib.parse.quote
    for di, df in janelas:
        raw = http(SND_AGENDA.format(di=q(di.strftime("%d/%m/%Y")), df=q(df.strftime("%d/%m/%Y"))), tentativas=3, timeout=180)
        if not raw:
            log(f"  SND agenda: falhou a janela {di}..{df}")
            return {}  # agenda parcial é pior que nenhuma: cai no modelo do cadastro
        for l in raw.decode("cp1252", errors="replace").replace("\r", "").split("\n"):
            r = [x.strip() for x in l.split("\t")]
            if len(r) < 7 or not re.match(r"\d{2}/\d{2}/\d{4}", r[0]):
                continue
            ev = norm(r[4])
            cod = ("J" if ev == "juros" else "I" if ev.startswith("incorporacao") else "P" if ev.startswith("premio")
                   else "V" if ev.startswith("vencimento") else "A" if ev == "amortizacao" else None)
            if not cod:
                continue  # amortização extraordinária (0%), participação, resgates: fora do fluxo contratual
            out.setdefault(r[3].upper(), []).append([r[1] or r[0], cod, num(r[6]) or 0.0,
                                                     "FIX" if "FIX" in r[5].upper() else "VAR"])
    for v in out.values():
        v.sort(key=lambda e: (datetime.strptime(e[0], "%d/%m/%Y"), "JIPAV".index(e[1])))
    log(f"  SND agenda: {sum(len(v) for v in out.values())} eventos de {len(out)} debêntures")
    return out


def fluxo_agenda(d: date, eventos: list, base: str, mult: float | None, R: float,
                 inicio: date | None, cv: "Curva | None", cupom_ref: float | None = None):
    """Fluxo por 1 unidade de saldo em d, evento a evento. Sem curva (cv=None): em termos do contrato
    (DI/IPCA se cancelam), para o spread implícito. Com curva: em R$ nominais projetados. Devolve
    [(t_anos, caixa, desconto_contratual)]."""
    evs = [(datetime.strptime(e[0], "%d/%m/%Y").date(), e[1], e[2], e[3]) for e in eventos]
    evs = [e for e in evs if e[0] > d]
    if not evs:
        return []
    jur = [e[0] for e in evs if e[1] in ("J", "I")]
    prev = jur[0] - (jur[1] - jur[0]) if len(jur) >= 2 else (inicio or d)
    if inicio and prev < inicio:
        prev = inicio
    prev = min(prev, d)
    m = (mult or 100) / 100
    di_curto = cv.pre(0.01) if cv else 0.0
    c_ref = cupom_ref if cupom_ref is not None else next((e[2] / 100 for e in evs if e[1] == "J"), 0.0)
    saldo = 1.0
    t_j = (prev - d).days / 365.25      # início do período de juros corrente (negativo = já acumulando)
    t_p = t_j
    ultimo_t = 0.0

    def fator(t0, t1, taxa):
        per = t1 - t0
        if base in ("DI+", "%DI") and cv:
            passado = (1 + di_curto) ** max(-t0, 0) if t0 < 0 else 1.0
            f_di = passado * cv.fator_di(t1) / (cv.fator_di(t0) if t0 > 0 else 1.0)
            if base == "%DI":
                mm = taxa / 100 if taxa and taxa > 20 else m
                return f_di ** mm
            return f_di * (1 + taxa / 100) ** per
        if base == "%DI":
            return 1.0
        return (1 + taxa / 100) ** per

    caixa_por_t: dict[float, float] = {}
    for dt, cod, taxa, bs in evs:
        t = (dt - d).days / 365.25
        ultimo_t = t
        caixa = 0.0
        if cod == "J":
            caixa = saldo * (fator(t_j, t, taxa) - 1)
            t_j = t
        elif cod == "I":                 # PIK: juros do período viram saldo
            saldo *= fator(t_j, t, taxa)
            t_j = t
        elif cod == "P":
            caixa = saldo * ((1 + taxa / 100) ** (t - t_p) - 1) if taxa else 0.0
            t_p = t
        elif cod == "A":
            amt = (taxa / 100) / R if bs == "FIX" else saldo * taxa / 100
            amt = min(max(amt, 0.0), saldo)
            saldo -= amt
            caixa = amt
        elif cod == "V":
            caixa = saldo
            saldo = 0.0
        if base in ("IPCA+", "IGPM+") and cv:
            caixa *= cv.fator_ipca(t)
        if caixa:
            caixa_por_t[t] = caixa_por_t.get(t, 0.0) + caixa
    if saldo > 1e-9:
        v = saldo * (cv.fator_ipca(ultimo_t) if base in ("IPCA+", "IGPM+") and cv else 1.0)
        caixa_por_t[ultimo_t] = caixa_por_t.get(ultimo_t, 0.0) + v
    res = []
    for t in sorted(caixa_por_t):
        if not cv:
            dc = 1 / (1 + c_ref) ** t
        elif base == "DI+":
            dc = 1 / (cv.fator_di(t) * (1 + c_ref) ** t)
        elif base == "%DI":
            dc = 1 / (cv.fator_di(t) ** m)
        elif base in ("IPCA+", "IGPM+"):
            dc = 1 / (cv.fator_ipca(t) * (1 + c_ref) ** t)
        else:
            dc = 1 / (1 + c_ref) ** t
        res.append((t, caixa_por_t[t], dc))
    return res


def saldo_relativo(snd: dict | None) -> float:
    """Fração do valor de emissão ainda em aberto (para amortizações em % do VNE)."""
    if snd and snd.get("vne") and snd.get("vn_atual") and \
            base_indexador(snd.get("indice"), snd.get("mult")) not in ("IPCA+", "IGPM+"):
        r = snd["vn_atual"] / snd["vne"]
        if 0.01 < r <= 1.0:
            return r
    return 1.0


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


YIELD_PCT_MIN, YIELD_PCT_MAX = 5.0, 150.0   # faixa de % do par em que yield/TIR são calculados


# ----------------------------------------------------------------------------- curva de juros (ETTJ ANBIMA)

ETTJ_URL = "https://www.anbima.com.br/informacoes/est-termo/CZ-down.asp"


def coletar_ettj(d: date) -> dict | None:
    corpo = urllib.parse.urlencode({"Idioma": "PT", "Dt_Ref": d.strftime("%d/%m/%Y"), "saida": "csv"}).encode()
    hdr = {"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded"}
    raw = None
    for i in range(3):
        try:
            req = urllib.request.Request(ETTJ_URL, data=corpo, headers=hdr)
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
            break
        except Exception as e:
            log(f"  ETTJ: {type(e).__name__} (tentativa {i + 1})")
            time.sleep(3)
    if not raw:
        return None
    ls = raw.decode("latin-1", errors="replace").replace("\r", "").split("\n")
    par = {}
    vert = []
    for l in ls:
        p = l.split(";")
        if p[0].strip().upper() in ("PREFIXADOS", "IPCA") and len(p) >= 7:
            par["pre" if p[0].strip().upper() == "PREFIXADOS" else "ipca"] = [num(x) for x in p[1:7]]
        elif len(p) >= 2 and re.fullmatch(r"[\d.]+", p[0].strip() or "x"):
            vert.append([num(p[0]), num(p[1]), num(p[2]) if len(p) > 2 else None, num(p[3]) if len(p) > 3 else None])
    if "pre" not in par or "ipca" not in par or None in par["pre"] + par["ipca"]:
        return None
    return {"data": d.isoformat(), "pre": par["pre"], "ipca": par["ipca"], "vertices": vert}


def obter_curva(d: date, baixar: bool = True) -> dict | None:
    pasta = DADOS / "curva"
    f = pasta / f"{d.isoformat()}.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    if baixar:
        c = coletar_ettj(d)
        if c:
            escrever_json(f, c)
            escrever_json(DADOS / "curva.json", c) if d >= max([date.fromisoformat(x.stem) for x in pasta.glob("*.json")] + [d]) else None
            log(f"  ETTJ: curva de {d} salva")
            return c
    # sem curva da data: usa a mais próxima disponível (anterior, senão posterior)
    arqs = sorted(pasta.glob("*.json")) if pasta.exists() else []
    if not arqs:
        return None
    ant = [a for a in arqs if a.stem <= d.isoformat()]
    escolha = ant[-1] if ant else arqs[0]
    return json.loads(escolha.read_text(encoding="utf-8"))


def _svensson(p, t):
    b1, b2, b3, b4, l1, l2 = p
    t = max(t, 1e-4)
    x1, x2 = l1 * t, l2 * t
    f1 = (1 - math.exp(-x1)) / x1
    f2_ = (1 - math.exp(-x2)) / x2
    return b1 + b2 * f1 + b3 * (f1 - math.exp(-x1)) + b4 * (f2_ - math.exp(-x2))


class Curva:
    def __init__(self, c: dict):
        self.p_pre, self.p_ipca = c["pre"], c["ipca"]
        self.data = c.get("data")

    def pre(self, t):
        return _svensson(self.p_pre, t)

    def ipca(self, t):
        return _svensson(self.p_ipca, t)

    def fator_di(self, t):          # DI acumulado de hoje até t (anos)
        return (1 + self.pre(t)) ** t if t > 0 else 1.0

    def fator_ipca(self, t):        # inflação implícita acumulada de hoje até t
        return ((1 + self.pre(t)) / (1 + self.ipca(t))) ** t if t > 0 else 1.0


def _data(txt):
    try:
        return datetime.strptime((txt or "").strip(), "%d/%m/%Y").date()
    except ValueError:
        return None


def _meses(cada, un) -> int | None:
    n = num(cada)
    if not n:
        return None
    u = norm(un)
    if u.startswith("ano"):
        return int(n * 12)
    if u.startswith("dia"):
        return max(int(round(n / 30)), 1)
    return int(n)


def _soma_meses(d0: date, m: int) -> date:
    a, mm = divmod(d0.month - 1 + m, 12)
    ano, mes = d0.year + a, mm + 1
    for dia in (d0.day, 30, 29, 28):
        try:
            return date(ano, mes, dia)
        except ValueError:
            continue
    return date(ano, mes, 28)


def _datas(primeira: date | None, passo: int | None, venc: date, ref: date) -> list[date]:
    """Datas de evento de 'primeira' até 'venc', a cada 'passo' meses, desde a última antes de ref."""
    if not passo or passo <= 0:
        return [venc]
    if not primeira:
        # sem data de carência: conta para trás a partir do vencimento
        ds, x = [], venc
        while x > ref - timedelta(days=400):
            ds.append(x)
            x = _soma_meses(venc, -passo * len(ds))
        return sorted(ds)
    ds, k = [], 0
    while True:
        x = _soma_meses(primeira, passo * k)
        if x >= venc:
            break
        ds.append(x)
        k += 1
        if k > 1200:
            break
    ds.append(venc)
    return ds


def fluxo_caixa(d: date, venc: date, cupom: float, juros_datas: list[date], amort_datas: list[date],
                inicio: date | None = None):
    """Fluxo por 1 unidade de principal em aberto em d. Juros compostos (base 252 ~ dias corridos/365.25).
    Amortizações futuras dividem o saldo em partes iguais (aproximação do cronograma)."""
    futuras_j = [x for x in juros_datas if x > d]
    passadas_j = [x for x in juros_datas if x <= d]
    futuras_a = [x for x in amort_datas if x > d] or [venc]
    eventos = sorted(set(futuras_j) | set(futuras_a) | {venc})
    # período corrente começa no último pagamento de juros; em carência (nenhum pagamento ainda),
    # começa no início da rentabilidade: os juros acumulados são pagos na primeira data
    ult = passadas_j[-1] if passadas_j else (inicio if inicio and inicio < d else None)
    saldo, cf = 1.0, []
    parcela = 1.0 / len(futuras_a)
    anterior = ult or d
    for x in eventos:
        juros = 0.0
        if x in futuras_j or x == venc:
            per = (x - anterior).days / 365.25
            juros = saldo * ((1 + cupom) ** per - 1)
            anterior = x
        amort = parcela if x in futuras_a else 0.0
        amort = min(amort, saldo)
        saldo -= amort
        cf.append(((x - d).days / 365.25, juros + amort))
    return cf


def _pv(cf, y):
    return sum(v / (1 + y) ** t for t, v in cf)


def ytm_pela_razao(cf, cupom: float, pct_par: float) -> float | None:
    """Taxa s tal que PV(s)/PV(cupom) = % do par. O DI (ou IPCA) se cancela: s sai como spread/juro real."""
    alvo = pct_par / 100 * _pv(cf, cupom)
    lo, hi = -0.5, 20.0
    if not (_pv(cf, hi) <= alvo <= _pv(cf, lo)):
        return None
    for _ in range(100):
        mid = (lo + hi) / 2
        if _pv(cf, mid) > alvo:
            lo = mid
        else:
            hi = mid
    return round(mid * 100, 2)


def ytm_descontado(fl, cupom: float, pct_par: float) -> float | None:
    """Spread/taxa s tal que PV(s)/PV(cupom) = % do par, descontando cada fluxo projetado pelo fator do contrato
    (DI futuro x (1+s)^t no DI+; inflação implícita x (1+s)^t no IPCA+). fl = [(t, caixa, dc)] com
    dc = desconto à taxa contratual. Diferente do método da razão simples, o DI não é cancelado: os cupons de um DI+
    carregam o DI, o que encurta a duration, igual à precificação da ANBIMA."""
    fl = [(t, v * dc) for t, v, dc in fl if v and dc]
    if not fl:
        return None
    pv = lambda s_: sum(w * ((1 + cupom) / (1 + s_)) ** t for t, w in fl)
    alvo = pct_par / 100 * pv(cupom)
    lo, hi = -0.5, 20.0
    if not (pv(hi) <= alvo <= pv(lo)):
        return None
    for _ in range(100):
        mid = (lo + hi) / 2
        if pv(mid) > alvo:
            lo = mid
        else:
            hi = mid
    return round(mid * 100, 2)


def fluxo_nominal(d: date, venc: date, base: str, c: float, mult: float | None,
                  juros_datas: list[date], amort_datas: list[date], cv: "Curva", inicio: date | None = None):
    """Fluxo nominal projetado (DI futuro e inflação implícita da curva) por 1 unidade de principal,
    e o fator de desconto contratual de cada data (para escalar até o PU par)."""
    futuras_j = [x for x in juros_datas if x > d]
    passadas_j = [x for x in juros_datas if x <= d]
    futuras_a = [x for x in amort_datas if x > d] or [venc]
    eventos = sorted(set(futuras_j) | set(futuras_a) | {venc})
    anterior = passadas_j[-1] if passadas_j else (inicio if inicio and inicio < d else d)
    di_curto = cv.pre(0.01)
    m = (mult or 100) / 100

    def acum_di(t):  # DI acumulado desde 'anterior' (passado aproximado pela taxa curta) até t (anos a partir de d)
        return cv.fator_di(t)

    saldo, parcela, out = 1.0, 1.0 / len(futuras_a), []
    t_ant = (anterior - d).days / 365.25  # <= 0
    for x in eventos:
        t = (x - d).days / 365.25
        juros = 0.0
        if x in futuras_j or x == venc:
            per = t - t_ant
            if base in ("DI+", "%DI"):
                passado = (1 + di_curto) ** max(-t_ant, 0) if t_ant < 0 else 1.0
                f_di = passado * acum_di(t) / (acum_di(t_ant) if t_ant > 0 else 1.0)
                g = f_di * (1 + c) ** per if base == "DI+" else f_di ** m
            else:
                g = (1 + c) ** per
            juros = saldo * (g - 1)
            t_ant = t
        amort = min(parcela if x in futuras_a else 0.0, saldo)
        saldo -= amort
        v = juros + amort
        if base in ("IPCA+", "IGPM+"):
            v *= cv.fator_ipca(t)
        # desconto contratual (taxa do papel sobre o indexador projetado)
        if base == "DI+":
            dc = 1 / (cv.fator_di(t) * (1 + c) ** t)
        elif base == "%DI":
            dc = 1 / (cv.fator_di(t) ** m)
        elif base in ("IPCA+", "IGPM+"):
            dc = 1 / (cv.fator_ipca(t) * (1 + c) ** t)
        else:
            dc = 1 / (1 + c) ** t
        out.append((t, v, dc))
    return out


def tir_moic(fluxo, pu_par: float, preco: float) -> dict:
    """Escala o fluxo unitário até o PU par e calcula TIR nominal, MOIC e retorno total para quem paga 'preco'."""
    pv_c = sum(v * dc for t, v, dc in fluxo)
    if pv_c <= 0 or not preco:
        return {}
    k = pu_par / pv_c
    cfs = [(t, v * k) for t, v, _ in fluxo]
    total = sum(v for _, v in cfs)
    lo, hi = -0.9, 50.0

    def pv(i):
        return sum(v / (1 + i) ** t for t, v in cfs)

    if not (pv(hi) <= preco <= pv(lo)):
        return {"moic": round(total / preco, 3), "ret": round((total / preco - 1) * 100, 1)}
    for _ in range(120):
        mid = (lo + hi) / 2
        if pv(mid) > preco:
            lo = mid
        else:
            hi = mid
    prazo = sum(t * v for t, v in cfs) / total if total else None
    return {"tir": round(mid * 100, 2), "moic": round(total / preco, 3),
            "ret": round((total / preco - 1) * 100, 1), "recebe": round(total, 2),
            "prazo": round(prazo, 2) if prazo else None}


def calcular_preco_e_yield(t: str | None, d: date, neg: dict | None, anb: dict | None,
                           snd: dict | None, curva: dict | None, cv: "Curva | None" = None,
                           ev: list | None = None) -> dict:
    """% do par e yield implícito (hold to maturity, pagando tudo no vencimento) do PU negociado."""
    pmed = (neg or {}).get("pu_med")
    # ANBIMA primeiro (calcula o par com VNA atualizado); SND como alternativa, pois em
    # alguns papéis o PU do SND vem igual ao valor de emissão, sem atualização
    pu_par = (anb or {}).get("pu_par") or (curva or {}).get("pu_par")
    fonte_par = "ANBIMA" if (anb or {}).get("pu_par") else ("SND" if (curva or {}).get("pu_par") else None)
    out = {"pu_par": pu_par, "fonte_par": fonte_par, "pct_par_neg": None, "y": None,
           "y_base": None, "y_est": False, "y_met": None, "tir": None, "moic": None, "ret": None,
           "prazo": None}
    if pmed and pu_par:
        out["pct_par_neg"] = round(pmed / pu_par * 100, 2)
    out["ref"] = "B3"
    if not pmed and (anb or {}).get("pu") and pu_par:
        pmed = anb["pu"]          # sem negócio: métricas no PU indicativo da ANBIMA
        out["ref"] = "ANBIMA"
    pct = round(pmed / pu_par * 100, 2) if pmed and pu_par else None
    if not pmed or pct is None or not (YIELD_PCT_MIN <= pct <= YIELD_PCT_MAX):
        return out
    # 0) debênture com agenda de eventos do SND: cronograma real, evento a evento
    if snd and ev:
        base = base_indexador(snd.get("indice"), snd.get("mult"))
        ini = _data(snd.get("inicio_rent")) or _data(snd.get("dt_emissao"))
        R = saldo_relativo(snd)
        if base:
            out["y_met"] = "agenda"
            if base != "%DI":
                c_ref = next((e[2] / 100 for e in ev if e[1] == "J"), (snd.get("taxa") or 0) / 100)
                y = None
                if base == "DI+" and cv:      # DI+ com curva: desconto pelo DI futuro (padrão ANBIMA)
                    y = ytm_descontado(fluxo_agenda(d, ev, base, snd.get("mult"), R, ini, cv, c_ref), c_ref, pct)
                if y is None:
                    cf = [(t_, v_) for t_, v_, _ in fluxo_agenda(d, ev, base, snd.get("mult"), R, ini, None)]
                    y = ytm_pela_razao(cf, c_ref, pct) if cf else None
                if y is not None:
                    out.update(y=y, y_base=base)
            else:
                out["y_base"] = "%DI"
            if cv:
                fl = fluxo_agenda(d, ev, base, snd.get("mult"), R, ini, cv)
                if fl:
                    out.update(tir_moic(fl, pu_par, pmed))
            if out.get("y") is not None or out.get("tir") is not None:
                return out
    # 1) debênture com cadastro SND: fluxo de caixa contratual (juros + amortização)
    if snd and snd.get("taxa") is not None:
        base = base_indexador(snd.get("indice"), snd.get("mult"))
        venc = _data(snd.get("venc"))
        if base and base != "%DI" and venc and venc > d:
            jm = _meses(snd.get("juros_cada"), snd.get("juros_un")) or 6
            jd = _datas(_data(snd.get("juros_car")), jm, venc, d)
            ad = [venc]
            if snd.get("amort"):
                am = _meses(snd.get("amort_cada"), snd.get("amort_un"))
                ad = _datas(_data(snd.get("amort_car")), am, venc, d) if am else [venc]
            ini = _data(snd.get("inicio_rent")) or _data(snd.get("dt_emissao"))
            y = None
            if base == "DI+" and cv:
                y = ytm_descontado(fluxo_nominal(d, venc, base, snd["taxa"] / 100, snd.get("mult"), jd, ad, cv, ini),
                                   snd["taxa"] / 100, pct)
            if y is None:
                cf = fluxo_caixa(d, venc, snd["taxa"] / 100, jd, ad, ini)
                y = ytm_pela_razao(cf, snd["taxa"] / 100, pct)
            if y is not None:
                out.update(y=y, y_base=base, y_met="fluxo")
            if cv:
                out.update(tir_moic(fluxo_nominal(d, venc, base, snd["taxa"] / 100, snd.get("mult"), jd, ad, cv, ini),
                                    pu_par, pmed))
            if y is not None:
                return out
        elif base == "%DI" and venc and venc > d and cv:
            # % do DI: sem spread contratual, mas a curva permite TIR/MOIC
            jm = _meses(snd.get("juros_cada"), snd.get("juros_un")) or 6
            jd = _datas(_data(snd.get("juros_car")), jm, venc, d)
            ad = [venc]
            if snd.get("amort"):
                am = _meses(snd.get("amort_cada"), snd.get("amort_un"))
                ad = _datas(_data(snd.get("amort_car")), am, venc, d) if am else [venc]
            ini = _data(snd.get("inicio_rent")) or _data(snd.get("dt_emissao"))
            out.update(tir_moic(fluxo_nominal(d, venc, "%DI", 0.0, snd.get("mult"), jd, ad, cv, ini), pu_par, pmed),
                       y_base="%DI")
            return out
    # 2) referência ANBIMA (taxa indicativa + PU + duration), boa perto do par
    if anb and anb.get("tx_ind") is not None and anb.get("pu") and anb.get("duration") and 70 <= pct <= 130:
        base = base_indexador(anb.get("indice"))
        if base and base != "%DI":
            y = yield_implicito(pmed, anb["pu"], anb["tx_ind"], anb["duration"])
            if y is not None:
                out.update(y=y, y_base=base, y_met="duration")
                cup = taxa_do_texto(anb.get("indice"))
                venc = _data(anb.get("vencimento"))
                if cv and cup is not None and venc and venc > d:
                    out.update(tir_moic(fluxo_nominal(d, venc, base, cup / 100, None, [venc], [venc], cv), pu_par, pmed))
                return out
    # 3) CRI/CRA sem cronograma: cupom do indexador ANBIMA, supondo pagamento bullet (estimativa)
    if anb and anb.get("indice") and anb.get("vencimento"):
        base = base_indexador(anb.get("indice"))
        cup = taxa_do_texto(anb.get("indice")) if base in ("DI+", "IPCA+", "IGPM+") else None
        venc = _data(anb.get("vencimento"))
        if base == "%DI" and cv and venc and venc > d:
            mm = re.search(r"([\d.,]+)\s*%", anb.get("indice") or "")
            mult = num(mm.group(1)) if mm else 100.0
            out.update(tir_moic(fluxo_nominal(d, venc, "%DI", 0.0, mult, [venc], [venc], cv), pu_par, pmed),
                       y_base="%DI", y_est=True)
            return out
        if base and base != "%DI" and cup is not None and venc and venc > d:
            cf = fluxo_caixa(d, venc, cup / 100, [venc], [venc])
            y = ytm_pela_razao(cf, cup / 100, pct)
            if y is not None:
                out.update(y=y, y_base=base, y_est=True, y_met="bullet")
            if cv:
                out.update(tir_moic(fluxo_nominal(d, venc, base, cup / 100, None, [venc], [venc], cv), pu_par, pmed))
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
        py.get("pu_par"), py.get("pct_par_neg"), py.get("y"), py.get("tir"),
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
    s = re.sub(r"(?<=[A-Z])['`´’](?=[A-Z])", "", s)      # D'OR -> DOR
    s = SUFIXOS.sub(" ", s)
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def empresa_do_ativo(t, cod, neg, anb_cc, snd, anb_deb) -> tuple[str | None, str | None]:
    """(empresa devedora, securitizadora). CRI/CRA: devedor vem da ANBIMA quando há."""
    if t in ("CRI", "CRA"):
        sec = (neg or {}).get("emissor") or (anb_cc or {}).get("securitizadora")
        dev = (anb_cc or {}).get("devedor")
        if not dev:   # sem devedor na ANBIMA: informe mensal da CVM (devedor/cedente com >= 50% do lastro)
            dev = (devedores_cricra().get((cod or "").upper()) or {}).get("nome")
        return (dev or sec), sec
    return ((snd or {}).get("empresa") or (neg or {}).get("emissor") or (anb_deb or {}).get("nome")), None


# ----------------------------------------------------------------------------- orquestração

_CACHE: dict = {}


def corrigir_juros_agenda(ag: dict, snd: dict) -> int:
    """A agenda do SND às vezes traz a taxa de juros futura como 0% ou 1% (sem a taxa definida). Se o cadastro tem
    outra taxa contratual, usa a do cadastro."""
    n = 0
    for c, ev in ag.items():
        t0 = (snd.get(c) or {}).get("taxa")
        if t0 is None:
            continue
        for k, e in enumerate(ev):
            if e[1] == "J" and e[2] in (0, 0.0, 1, 1.0) and abs(e[2] - t0) > 0.25:
                ev[k] = [e[0], e[1], t0, e[3]]
                n += 1
    if n:
        log(f"  juros da agenda corrigidos pela taxa do cadastro: {n} eventos")
    return n


def snd_agenda() -> dict:
    if "agenda" not in _CACHE:
        ag = coletar_snd_agenda(datetime.now(BRT).date())
        corrigir_juros_agenda(ag, snd_cadastro())
        _CACHE["ajustes"] = ajustar_pelas_escrituras(ag)
        _CACHE["agenda"] = ag
    return _CACHE["agenda"]


def devedores_cricra() -> dict:
    """Devedor de cada CRI/CRA pelos informes mensais da CVM (docs/data/devedores_cricra.json, coletor/devedores.py)."""
    if "dev" not in _CACHE:
        try:
            _CACHE["dev"] = json.loads((DADOS / "devedores_cricra.json").read_text(encoding="utf-8"))
        except Exception:
            _CACHE["dev"] = {}
    return _CACHE["dev"]


def garantias_lidas() -> dict:
    """Garantias por código, lidas nos documentos da CVM (campo "garantias" de docs/data/escrituras/<cnpj>_<em>.json)."""
    if "gar" in _CACHE:
        return _CACHE["gar"]
    out = {}
    d = DADOS / "escrituras"
    for p in sorted(d.glob("[0-9]*_*.json")) if d.exists() else []:
        try:
            j = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        for c, g in (j.get("garantias") or {}).items():
            gs = g.get("garantias") or []
            out[c.upper()] = {"res": g.get("resumo"), "esp": g.get("especie"), "n": len(gs), "arq": p.stem,
                              "tip": sorted({(x.get("tipo") or "outra").split("/")[0].strip().lower() for x in gs
                                             if (x.get("situacao") or "") != "liberada"})}
    _CACHE["gar"] = out
    return out


def nomes_empresas() -> dict:
    """Nome curto pelo qual cada empresa é conhecida (docs/data/nomes_empresas.json, gerado por coletor/nomes.py)."""
    if "nomes" not in _CACHE:
        p = DADOS / "nomes_empresas.json"
        try:
            _CACHE["nomes"] = {k: v["nome"] for k, v in json.loads(p.read_text(encoding="utf-8")).items() if v.get("nome")}
        except Exception:
            _CACHE["nomes"] = {}
    return _CACHE["nomes"]


def escrituras() -> dict:
    """Leituras de escrituras/aditamentos/atas (docs/data/escrituras), por código: série extraída, evento de crédito,
    conferência com o SND e arquivo de origem."""
    if "esc" in _CACHE:
        return _CACHE["esc"]
    out = {}
    for p in sorted((DADOS / "escrituras").glob("*_*.json")) if (DADOS / "escrituras").exists() else []:
        try:
            j = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        evs = j.get("eventos") or {}
        for sr in j.get("series", []):
            c = sr.get("codigo")
            if not c:
                continue
            out[c] = {"serie": sr, "evento": evs.get(c) or {}, "conf": sr.get("conferencia_snd") or {},
                      "arq": p.stem, "lido": (j.get("meta") or {}).get("lido_em")}
    _CACHE["esc"] = out
    return out


def ajustar_pelas_escrituras(agenda: dict) -> dict:
    """Corrige a agenda do SND com o cronograma lido nos documentos, só quando as datas batem (±5 dias) e a leitura
    não é de confiança baixa: (1) percentual ausente/zero no SND e presente no documento; (2) base da amortização
    (FIX = % do VNE; VAR = % do saldo) diferente da indicada no documento, com os mesmos percentuais."""
    hoje = datetime.now(BRT).date()
    ajustes = {}
    for c, x in escrituras().items():
        ev, sr = agenda.get(c), x["serie"]
        if not ev or sr.get("confianca") == "baixa":
            continue
        doc = []
        for a in sr.get("amortizacoes") or []:
            try:
                dd = date.fromisoformat(a["data"])
            except Exception:
                continue
            if dd > hoje and a.get("percentual") is not None:
                doc.append((dd, float(a["percentual"]), a.get("base")))
        if not doc:
            continue
        notas, usados = [], set()
        for k, e in enumerate(ev):
            if e[1] != "A":
                continue
            de = datetime.strptime(e[0], "%d/%m/%Y").date()
            if de <= hoje:
                continue
            cand = [(abs((dd - de).days), n) for n, (dd, _, _) in enumerate(doc) if n not in usados and abs((dd - de).days) <= 5]
            if not cand:
                continue
            n = min(cand)[1]
            usados.add(n)
            _, pct, bs = doc[n]
            base_doc = "VAR" if bs == "saldo" else "FIX" if bs == "vne" else None
            novo = list(e)
            if not e[2] and pct:
                novo[2] = pct
                notas.append("percentual")
            if base_doc and base_doc != (e[3] or "VAR") and abs((e[2] or 0) - pct) < 0.05:
                novo[3] = base_doc
                notas.append("base")
            ev[k] = novo
        if notas:
            ajustes[c] = sorted(set(notas))
    if ajustes:
        log(f"  agenda ajustada pelas escrituras: {len(ajustes)} títulos")
    return ajustes


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
    cj = obter_curva(d)
    cv = Curva(cj) if cj else None
    agenda = snd_agenda()
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
        cvp = curva.get(cod) if t == "DEB" else None
        py = calcular_preco_e_yield(t, d, n, a, s, cvp, cv, agenda.get(cod) if t == "DEB" else None)
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
                   "snd_pu": len(curva), "curva": (cj or {}).get("data")},
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


def recalcular() -> None:
    """Refaz % do par, yield e alertas de todos os dias já coletados, sem baixar a B3 de novo."""
    snd = snd_cadastro()
    arquivos = sorted((DADOS / "daily").glob("*.json"))
    for f in arquivos:
        dd = json.loads(f.read_text(encoding="utf-8"))
        d = date.fromisoformat(dd["data"])
        cj = obter_curva(d)
        cv = Curva(cj) if cj else None
        agenda = snd_agenda()
        for a in dd["ativos"]:
            p_ant = a.get("p") or {}
            cvp = {"pu_par": p_ant.get("pu_par")} if p_ant.get("fonte_par") == "SND" else None
            s = snd.get(a["c"]) if a.get("t") == "DEB" else None
            py = calcular_preco_e_yield(a.get("t"), d, a.get("n"), a.get("a"), s, cvp, cv,
                                        agenda.get(a["c"]) if a.get("t") == "DEB" else None)
            a["p"] = py
            serie = atualizar_serie(a["c"], dd["data"], a.get("n"), a.get("a"), py)
            a["al"] = alertas(a.get("n"), a.get("a"), py, serie, dd["data"])
        escrever_json(f, dd)
    log(f"Recalculado: {len(arquivos)} dias")


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
                pp_ = a.get("p") or {}
                ultimo_neg[a["c"]] = (ds, a["n"].get("pu_med"), a["n"].get("volume"),
                                      pp_.get("pct_par_neg"), pp_.get("y"), pp_.get("y_base"), pp_.get("y_est"),
                                      pp_.get("tir"), pp_.get("moic"), pp_.get("ret"), pp_.get("prazo"))
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
        dv = devedores_cricra().get(c.upper()) or {} if t in ("CRI", "CRA") else {}
        if t in ("CRI", "CRA") and not anb.get("devedor") and dv.get("nome") and dv.get("tipo") in ("devedor", "cedente"):
            emp = dv["nome"]          # devedor do informe mensal da CVM no lugar da securitizadora
        idx_txt = anb.get("indice")
        if not idx_txt and s:
            base = base_indexador(s.get("indice"), s.get("mult"))
            if base == "%DI":
                idx_txt = f"{f2(s.get('mult'))}% do DI"
            elif base == "PRE":
                idx_txt = f"{f2(s.get('taxa'))}% a.a."
            elif base:
                idx_txt = f"{base[:-1]} + {f2(s.get('taxa'))}%"
        ag = snd_agenda().get(c) or []
        ven_ag = next((e[0] for e in ag if e[1] == "V"), ag[-1][0] if ag else None)
        itens.append({
            "c": c, "t": t, "e": emp, "k": chave_empresa(emp), "s": a.get("s"),
            "ven": next((v for v in (anb.get("vencimento"), ven_ag, (s or {}).get("venc"))
                         if v and not (v[-4:].isdigit() and int(v[-4:]) >= 2090)),
                        anb.get("vencimento") or (s or {}).get("venc")),
            "idx": idx_txt, "gar": (s or {}).get("garantia"), "inc": (s or {}).get("incentivada"),
            "em": (s or {}).get("emissao"), "se": (s or {}).get("serie"),
            "qm": (s or {}).get("qtd_mercado"), "vne": (s or {}).get("vne"),
            "cnpj": (s or {}).get("cnpj"), "ra": (s or {}).get("resgate_antecipado"),
            "par": (h.get("p") or {}).get("pu_par"),
            "ud": un[0] if un else None, "up": un[1] if un else None, "uv": un[2] if un else None,
            "upp": un[3] if un else None, "uy": un[4] if un else None,
            "uyb": un[5] if un else None, "uye": un[6] if un else None,
            "utir": un[7] if un else (h.get("p") or {}).get("tir"),
            "umoic": un[8] if un else (h.get("p") or {}).get("moic"),
            "uret": un[9] if un else (h.get("p") or {}).get("ret"),
            "uprz": un[10] if un else (h.get("p") or {}).get("prazo"),
            "uind": not un and (h.get("p") or {}).get("ref") == "ANBIMA",
            "uy": un[4] if un else (h.get("p") or {}).get("y"),
            "uyb": un[5] if un else (h.get("p") or {}).get("y_base"),
            "vh": (h.get("n") or {}).get("volume"),
            "ta": anb.get("tx_ind"), "pa": anb.get("pu"), "ppa": anb.get("pct_par"),
            "du": anb.get("duration"), "cat": anb.get("categoria"), "al": h.get("al") or [],
        })
        if t in ("CRI", "CRA"):
            if anb.get("devedor"):
                itens[-1]["dvf"] = "ANBIMA"
            elif dv.get("tipo") in ("devedor", "cedente") and dv.get("nome"):
                itens[-1].update(dvf="CVM", dvp=dv.get("pct"), dvt=dv["tipo"])
                if not itens[-1].get("cnpj") and dv.get("cnpj"):
                    itens[-1]["cnpj"] = dv["cnpj"]
            elif dv.get("tipo") == "pulverizado" and itens[-1]["e"] == itens[-1].get("s"):
                itens[-1]["pulv"] = True
        gl = garantias_lidas().get(c.upper())
        if gl:
            itens[-1].update(gres=gl["res"], gesp=gl["esp"], gn=gl["n"], gtip=gl["tip"], garq=gl["arq"])
            if gl["n"] == 0 and ((s or {}).get("garantia") == "Real" or
                                 re.search(r"n[aã]o (determin|identific|localiz)|sem documento|n[aã]o h[aá] documento", gl["res"] or "", re.I)):
                itens[-1]["gind"] = True        # leitura não identificou a garantia (não é "sem garantia")
        nm = nomes_empresas().get(itens[-1]["k"])
        if nm:
            itens[-1]["n"] = nm
        x = escrituras().get(c)
        if x:
            ev_ = x["evento"]
            sig = {"vencimento_antecipado": "va", "rj": "rj", "re": "re", "reperfilamento": "rp"}.get(ev_.get("evento"))
            cf = x["conf"]
            itens[-1].update({
                "cr": sig, "crd": ev_.get("data") if sig else None, "crr": ev_.get("resumo") if sig else None,
                "esc": x["arq"],
                "escok": True if cf.get("ok") else (False if cf.get("snd") and cf.get("amort_doc") and cf.get("amort_snd") else None),
                "aj": (_CACHE.get("ajustes") or {}).get(c),
            })
    # mesma empresa com grafias diferentes (ANBIMA, SND, Receita): unifica pelo CNPJ, usando a grafia das debêntures
    por_cnpj = collections.defaultdict(list)
    for it in itens:
        cj = re.sub(r"\D", "", it.get("cnpj") or "")
        if cj and it.get("k"):
            por_cnpj[cj].append(it)
    unif = 0
    for cj, its in por_cnpj.items():
        if len({x["k"] for x in its}) < 2:
            continue
        base_ = collections.Counter((x["k"], x["e"]) for x in its if x["t"] == "DEB") or collections.Counter((x["k"], x["e"]) for x in its)
        k0, e0 = base_.most_common(1)[0][0]
        n0 = nomes_empresas().get(k0)
        for x in its:
            if x["k"] != k0:
                x["k"], x["e"] = k0, e0
                if n0:
                    x["n"] = n0
                unif += 1
    if unif:
        log(f"  títulos unificados pelo CNPJ: {unif}")
    # grupos econômicos (coletor/grupos.json): títulos de controladas aparecem na página da controladora
    try:
        grupos = {k: v for k, v in json.loads((RAIZ / "coletor" / "grupos.json").read_text(encoding="utf-8")).items()
                  if not k.startswith("_")}
    except Exception:
        grupos = {}
    if grupos:
        nome_k = {}
        for it in itens:
            nome_k.setdefault(it["k"], (it["e"], it.get("n")))
        for it in itens:
            g = grupos.get(it.get("k"))
            if g and g["grupo"] in nome_k and g["grupo"] != it["k"]:
                it["leg"] = it.get("n") or it["e"]          # emissora/devedora legal
                it["legr"] = g.get("relacao")
                it["k"] = g["grupo"]
                it["n"] = nome_k[g["grupo"]][1] or it.get("n")
    try:
        lista = json.loads((RAIZ / "coletor" / "lista_acompanhamento.json").read_text(encoding="utf-8"))["empresas"]
    except Exception:
        lista = []
    for it in itens:
        alvo = chave_empresa(it.get("e")) + " " + chave_empresa(it.get("s"))
        for emp in lista:
            if any(chave_empresa(t) and re.search(r"(^|\s)" + re.escape(chave_empresa(t)) + r"(\s|$)", alvo)
                   for t in emp["termos"]):
                it["w"] = emp["nome"]
                it["ws"] = emp.get("situacao")
                # evento informado na lista: "substitui" corrige a leitura dos documentos; "completa" só preenche quem ficou sem selo
                if emp.get("evento") and (emp.get("evento_modo") == "substitui" or not it.get("cr")):
                    it["cr"] = emp["evento"] if emp["evento"] != "nenhum" else None
                    it["crd"], it["crr"], it["crf"] = emp.get("evento_data"), emp.get("evento_resumo"), "lista"
                break
    escrever_cronogramas(itens, snd, datas[-1])
    cur = sorted((DADOS / "curva").glob("*.json")) if (DADOS / "curva").exists() else []
    if cur:
        escrever_json(DADOS / "curva.json", json.loads(cur[-1].read_text(encoding="utf-8")))
    escrever_json(DADOS / "catalogo.json", {"data": datas[-1], "itens": itens})
    log(f"Catálogo: {len(itens)} títulos, {len({i['k'] for i in itens})} empresas")


def escrever_cronogramas(itens: list, snd: dict, ds: str) -> None:
    """docs/data/cronograma/<COD>.json: eventos da agenda + fluxo projetado em R$ por debênture (curva do dia)."""
    agenda = snd_agenda()
    if not agenda:
        return
    d = date.fromisoformat(ds)
    cj = obter_curva(d, baixar=False)
    cv = Curva(cj) if cj else None
    pasta = DADOS / "cronograma"
    n = 0
    for i in itens:
        ev, s = agenda.get(i["c"]), snd.get(i["c"])
        if not ev or not s:
            continue
        base = base_indexador(s.get("indice"), s.get("mult"))
        fluxo = []
        if cv and base and i.get("par"):
            fl = fluxo_agenda(d, ev, base, s.get("mult"), saldo_relativo(s),
                              _data(s.get("inicio_rent")) or _data(s.get("dt_emissao")), cv)
            pv_c = sum(v * dc for _, v, dc in fl)
            if pv_c > 0:
                k = i["par"] / pv_c
                fluxo = [[(d + timedelta(days=round(t * 365.25))).isoformat(), round(v * k, 4)] for t, v, _ in fl]
        obj = {"c": i["c"], "base": base, "ev": ev, "fluxo": fluxo, "curva": (cj or {}).get("data"), "par": i.get("par"),
               "aj": (_CACHE.get("ajustes") or {}).get(i["c"]), "escok": i.get("escok")}
        f = pasta / f"{i['c']}.json"
        novo = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        if not f.exists() or f.read_text(encoding="utf-8") != novo:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(novo, encoding="utf-8")
            n += 1
    log(f"  cronogramas atualizados: {n}")


def f2(v) -> str:
    return "" if v is None else f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", help="AAAA-MM-DD (padrão: hoje em Brasília)")
    ap.add_argument("--backfill", type=int, default=0,
                    help="também processa os N dias úteis anteriores")
    ap.add_argument("--forcar", action="store_true", help="sobrescreve o arquivo do dia")
    ap.add_argument("--recalcular", action="store_true", help="só refaz par/yield dos dias já coletados")
    args = ap.parse_args()
    if args.recalcular:
        recalcular()
        construir_catalogo()
        return 0

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
