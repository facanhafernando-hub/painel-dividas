"""Auditoria geral do catálogo (docs/data/catalogo.json). Não chama API paga.

Grava docs/data/_debug/auditoria.json e imprime um resumo. Checagens:
  preco      % do par vs indicativo ANBIMA, extremos, negócio defasado, negócio sem PU par
  yield      yield vs taxa ANBIMA quando os preços batem, yield faltando, yield implausível
  emissor    título fora da curva do próprio emissor (mesmo indexador)
  empresa    mesmo CNPJ em páginas diferentes, título na securitizadora, nome curto faltando
  evento     título abaixo de 50% do par sem evento de crédito
  garantia   cobertura e conflito espécie SND × leitura
  par        PU par incoerente com o VNE
"""
from __future__ import annotations

import collections
import json
import statistics
import sys
from datetime import date
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
DADOS = RAIZ / "docs" / "data"


def base(i):
    t = (i.get("idx") or "").upper()
    if "IPCA" in t or "IGP" in t:
        return "IPCA+"
    if "% DO DI" in t or "%DI" in t or "DO CDI" in t:
        return "%DI"
    if "DI" in t:
        return "DI+"
    if "PRE" in t or "A.A." in t:
        return "PRE"
    return None


def dias(d1, d2):
    return (date.fromisoformat(d2) - date.fromisoformat(d1)).days


def main():
    cat = json.loads((DADOS / "catalogo.json").read_text(encoding="utf-8"))
    itens = cat["itens"]
    hoje = json.loads((DADOS / "index.json").read_text(encoding="utf-8"))["ultima"]
    out = collections.defaultdict(list)

    def add(tipo, i, msg, **kw):
        out[tipo].append({"c": i["c"], "t": i["t"], "emp": i.get("n") or i.get("e"), "k": i.get("k"), "msg": msg, **kw})

    for i in itens:
        upp, ppa, par, vne = i.get("upp"), i.get("ppa"), i.get("par"), i.get("vne")
        p = upp if upp is not None else ppa
        # preço
        if upp is not None and ppa is not None and abs(upp - ppa) > 10:
            add("preco_vs_anbima", i, f"negócio {upp:.1f}% × ANBIMA {ppa:.1f}% do par", ud=i.get("ud"), uv=i.get("uv"), dif=round(upp - ppa, 1))
        if p is not None and (p > 115 or p < 15):
            add("preco_extremo", i, f"{p:.1f}% do par", ud=i.get("ud"), uv=i.get("uv"))
        if upp is not None and ppa is not None and i.get("ud") and dias(i["ud"], hoje) > 14 and abs(upp - ppa) > 5:
            add("negocio_defasado", i, f"último negócio {i['ud']} a {upp:.1f}% × ANBIMA hoje {ppa:.1f}%", dif=round(upp - ppa, 1))
        if i.get("ud") and p is None:
            add("negocio_sem_par", i, "tem negócio mas não tem PU par (sem % do par)", uv=i.get("uv"))
        # par
        if par and vne and vne >= 100 and (par / vne > 4 or par / vne < 0.02):
            add("par_incoerente", i, f"PU par {par:.2f} × VNE {vne:.0f}")
        # yield
        b = base(i)
        if p is not None and 5 <= p <= 150 and i.get("uy") is None and b in ("DI+", "IPCA+", "PRE"):
            add("yield_faltando", i, f"{p:.1f}% do par sem yield ({i.get('idx')})")
        if upp is not None and ppa is not None and abs(upp - ppa) <= 2 and i.get("uy") is not None and i.get("ta") is not None \
                and b in ("DI+", "IPCA+", "PRE") and abs(i["uy"] - i["ta"]) > 1.5 and not i.get("uye"):
            add("yield_vs_anbima", i, f"yield {i['uy']:.2f} × taxa ANBIMA {i['ta']:.2f} com preços iguais ({upp:.1f}/{ppa:.1f})", dif=round(i["uy"] - i["ta"], 2))
        # evento
        if p is not None and p < 50 and not i.get("cr"):
            add("abaixo50_sem_evento", i, f"{p:.1f}% do par sem evento de crédito", ud=i.get("ud"))
        # garantia
        g = (i.get("gar") or "").lower()
        if i.get("gn") is not None and "real" in g and not any("fiduci" in x or "penhor" in x or "hipoteca" in x or "real" in x for x in (i.get("gtip") or [])) and i.get("gn", 0) > 0:
            add("garantia_conflito", i, f"SND diz '{i.get('gar')}', leitura achou {i.get('gtip')}")

    # coerência dentro do emissor (mesmo indexador, títulos com yield e preço entre 30% e 120%)
    por = collections.defaultdict(list)
    for i in itens:
        p = i.get("upp") if i.get("upp") is not None else i.get("ppa")
        if i.get("uy") is not None and p is not None and 30 <= p <= 120 and base(i) in ("DI+", "IPCA+") and not i.get("uye"):
            por[(i.get("k"), base(i))].append(i)
    for (k, b), lst in por.items():
        if len(lst) < 3:
            continue
        med = statistics.median(x["uy"] for x in lst)
        lim = 4.0 if b == "DI+" else 5.0
        for x in lst:
            if abs(x["uy"] - med) > max(lim, abs(med) * 0.6):
                add("fora_da_curva_emissor", x, f"yield {x['uy']:.2f} × mediana do emissor {med:.2f} ({b}, {len(lst)} títulos)", dif=round(x["uy"] - med, 2))

    # empresa
    cnpj_k = collections.defaultdict(set)
    for i in itens:
        if i.get("cnpj") and not i.get("leg"):
            cnpj_k[i["cnpj"]].add(i.get("k"))
    for cj, ks in cnpj_k.items():
        if len(ks) > 1:
            out["cnpj_em_varias_paginas"].append({"cnpj": cj, "ks": sorted(k or "" for k in ks)})
    sec = collections.Counter(i.get("k") for i in itens if i["t"] in ("CRI", "CRA") and i.get("s") and i.get("e") == i.get("s"))
    out["na_securitizadora"] = [{"k": k, "n": n} for k, n in sec.most_common()]
    out["sem_nome_curto"] = sorted({i.get("k") for i in itens if i.get("k") and not i.get("n")})

    resumo = {k: len(v) for k, v in out.items()}
    cobertura = {
        "itens": len(itens),
        "com_negocio_30d": sum(1 for i in itens if i.get("ud")),
        "com_pct_par": sum(1 for i in itens if (i.get("upp") if i.get("upp") is not None else i.get("ppa")) is not None),
        "com_yield": sum(1 for i in itens if i.get("uy") is not None),
        "garantia_lida": sum(1 for i in itens if i.get("gn") is not None),
        "com_evento": collections.Counter(i.get("cr") for i in itens if i.get("cr")),
    }
    (DADOS / "_debug").mkdir(exist_ok=True)
    (DADOS / "_debug" / "auditoria.json").write_text(json.dumps({"data": hoje, "resumo": resumo, "cobertura": cobertura, "casos": out},
                                                               ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(json.dumps({"resumo": resumo, "cobertura": cobertura}, ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    sys.exit(main())
