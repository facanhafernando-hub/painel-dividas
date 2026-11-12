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
import unicodedata
import urllib.error
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
         tentativas: int = 3) -> bytes | None:
    hdr = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "pt-BR,pt;q=0.9"}
    if corpo is not None:
        hdr["Content-Type"] = "application/json"
    for i in range(1, tentativas + 1):
        try:
            req = urllib.request.Request(url, data=corpo, headers=hdr, method=metodo)
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            log(f"  HTTP {e.code} em {url} (tentativa {i})")
        except Exception as e:  # rede, timeout, TLS
            log(f"  erro {type(e).__name__}: {e} em {url} (tentativa {i})")
        time.sleep(3 * i)
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
    "tipo":      ["instrumentofinanceiro", "instrumento", "financialinstrument", "instrmtp",
                  "instrmnttp", "tipoinstrumento", "tipo", "sctyctgynm", "sctyctgy"],
    "codigo":    ["codigoif", "codif", "tickersymbol", "tckrsymb", "codigo", "ifcode"],
    "isin":      ["codigoisin", "isin", "isincode"],
    "emissor":   ["emissor", "issuer", "issrnm", "issuername", "nomeemissor", "emissornm"],
    "data_neg":  ["datanegocio", "tradedate", "trddt", "traddt", "rptdt"],
    "data_liq":  ["dataliquidacao", "settlementdate", "sttlmdt"],
    "qtd":       ["quantidadenegociada", "tradedquantity", "tradqty", "trdqty", "quantidade"],
    "pu_min":    ["precominimo", "minprice", "minpric", "minprc"],
    "pu_med":    ["precomedio", "averageprice", "avrgpric", "avgpric", "avgprc"],
    "pu_max":    ["precomaximo", "maxprice", "maxpric", "maxprc"],
    "pu_ult":    ["ultimopreco", "lastprice", "lastpric", "lastprc"],
    "pu_ref":    ["precodereferencia", "precoreferencia", "referenceprice", "refpric", "mtmpric"],
    "n_neg":     ["numerodenegocios", "numberoftrades", "nbroftrds", "nboftrds", "tradsqty", "qtdnegocios",
                  "rglrtxsqty"],
    "volume":    ["volumefinanceiror", "volumefinanceiro", "financialvolume", "ntlfinvol",
                  "fnclvol", "volume"],
    "classif":   ["classificacaodonegocio", "classificacaodosnegocios", "classificacao",
                  "tradeclassification", "trdclssfctn"],
    "oscilacao": ["oscilacao", "oscillation", "oscn", "prcvartn"],
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


def b3_tabela(tabela: str, d: date) -> tuple[list[str], list[list]]:
    """Lê todas as páginas de uma tabela do BDI. Protege contra página 'eco'."""
    colunas: list[str] = []
    linhas: list[list] = []
    ultimo_hash = None
    for pag in range(1, 200):
        raw = http(B3_URL.format(tabela=tabela, d=d.isoformat(), pag=pag, tam=1000),
                   metodo="POST", corpo=b"{}")
        if raw is None:
            break
        try:
            t = json.loads(raw).get("table") or {}
        except json.JSONDecodeError:
            log("  B3 devolveu algo que não é JSON")
            break
        vals = t.get("values") or []
        if not colunas:
            colunas = [c.get("name") or c.get("friendlyName") or f"c{i}"
                       for i, c in enumerate(t.get("columns") or [])]
            escrever_json(DADOS / "_debug" / f"b3_{tabela}_colunas.json",
                          {"data": d.isoformat(), "columns": t.get("columns"),
                           "amostra": vals[:3]})
        if not vals:
            break
        h = hashlib.sha1(json.dumps(vals, sort_keys=True).encode()).hexdigest()
        if h == ultimo_hash:  # endpoint repetindo a mesma página
            break
        ultimo_hash = h
        linhas.extend(vals)
        if len(vals) < 1000:
            break
    return colunas, linhas


def coletar_b3(d: date) -> dict[str, dict]:
    colunas, linhas = b3_tabela("ConsolidatedRecords", d)
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
    for row in linhas:
        cod = str(g(row, "codigo") or "").strip().upper()
        if not cod or cod == "-":
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
    log(f"  B3: {len(linhas)} linhas, {len(ativos)} ativos negociados")
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


# ----------------------------------------------------------------------------- séries e alertas

def carregar_serie(cod: str) -> list[list]:
    f = DADOS / "series" / f"{cod}.json"
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))["p"]
        except Exception:
            return []
    return []


# ponto da série: [data, pu_med, qtd, volume, n_neg, pu_anbima, tx_ind, pct_par]
def atualizar_serie(cod: str, d: str, neg: dict | None, anb: dict | None) -> list[list]:
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
    ])
    pts.sort(key=lambda p: p[0])
    pts = pts[-SERIE_MAX_PONTOS:]
    escrever_json(DADOS / "series" / f"{cod}.json", {"c": cod, "p": pts})
    return pts


def alertas(neg: dict | None, anb: dict | None, serie: list[list], ds: str) -> list[str]:
    out = []
    if anb and anb.get("categoria") and "VENCID" in anb["categoria"]:
        out.append("vencida_antecipadamente")
    pu_par = anb.get("pu_par") if anb else None
    pmed = neg.get("pu_med") if neg else None
    if pmed and pu_par and pmed / pu_par * 100 < ALERTA_PCT_PAR:
        out.append("negociado_abaixo_par")
    elif anb and anb.get("pct_par") is not None and anb["pct_par"] < ALERTA_PCT_PAR:
        out.append("indicativo_abaixo_par")
    if pmed and anb and anb.get("pu"):
        if abs(pmed / anb["pu"] - 1) * 100 > ALERTA_DESVIO_ANBIMA:
            out.append("desvio_anbima")
    if neg and neg.get("volume"):
        hist = [p[3] for p in [q for q in serie if q[0] < ds][-20:] if p[3]]
        media = sum(hist) / 20 if hist else 0  # dias sem negócio contam como zero
        if neg["volume"] >= ALERTA_VOLUME_MIN and (not media or neg["volume"] > ALERTA_VOLUME_MULT * media):
            out.append("salto_volume")
    return out


# ----------------------------------------------------------------------------- orquestração

def processar(d: date, forcar: bool = False) -> bool:
    ds = d.isoformat()
    destino = DADOS / "daily" / f"{ds}.json"
    log(f"Data {ds}")
    neg = coletar_b3(d)
    anb = coletar_anbima(d)
    if not neg and not anb:
        log("  sem dados (feriado, fim de semana ou ainda não publicado)")
        return False
    if destino.exists() and not forcar:
        antigo = json.loads(destino.read_text(encoding="utf-8"))
        # não regredir: se uma fonte falhou agora mas existia antes, mantém o anterior
        if (not neg and antigo["fontes"]["b3"]) or (not anb and antigo["fontes"]["anbima"]):
            log("  uma fonte veio vazia e o arquivo existente é mais completo; mantendo")
            return False

    ativos = []
    for cod in sorted(set(neg) | set(anb)):
        n, a = neg.get(cod), anb.get(cod)
        serie = atualizar_serie(cod, ds, n, a)
        reg = {
            "c": cod,
            "t": (n or {}).get("tipo") or ("DEB" if a else None),
            "e": (n or {}).get("emissor") or (a or {}).get("nome"),
            "i": (n or {}).get("isin"),
            "n": {k: n[k] for k in ("qtd", "volume", "n_neg", "pu_min", "pu_med", "pu_max",
                                    "pu_ult", "pu_ref", "oscilacao", "vol_extragrupo",
                                    "vol_intragrupo")} if n else None,
            "a": {k: a.get(k) for k in ("vencimento", "indice", "tx_compra", "tx_venda",
                                        "tx_ind", "int_min", "int_max", "pu", "pct_par",
                                        "pu_par", "duration", "categoria")} if a else None,
        }
        reg["al"] = alertas(n, a, serie, ds)
        ativos.append(reg)

    escrever_json(destino, {
        "data": ds,
        "gerado_em": datetime.now(BRT).isoformat(timespec="seconds"),
        "fontes": {"b3": len(neg), "anbima": len(anb)},
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
    for _ in range(args.backfill if args.backfill else (0 if datas else 1)):
        x = dia_util_anterior(x)
        datas.append(x)
    # do mais antigo para o mais novo, para os alertas de volume enxergarem o histórico
    ok = [processar(dd, args.forcar) for dd in sorted(datas)]
    log(f"Fim: {sum(ok)}/{len(ok)} datas gravadas")
    return 0


if __name__ == "__main__":
    sys.exit(main())
