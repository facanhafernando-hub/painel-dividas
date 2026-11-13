"""Verificação do yield implícito de todos os títulos do último pregão, por três caminhos:

 1. Reprodução: recalcula com o próprio motor do painel (calcular_preco_e_yield) e compara com o valor publicado.
 2. Implementação independente: refaz o fluxo contratual a partir da agenda com código separado (juros antes de
    amortização na mesma data, saldo explícito), resolve com Newton e confere que o preço volta ao % do par.
    Para títulos sem agenda (fluxo do cadastro, duration ANBIMA, bullet), confere o resíduo de preço.
 3. Mercado: calcula o yield no PU indicativo da ANBIMA com o fluxo do painel e compara com a taxa indicativa
    publicada pela ANBIMA (só debêntures DI+, IPCA+ e prefixadas com agenda).

Saída: docs/data/_debug/verificacao_yield.json + resumo no log.
"""
import json, math, sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import coletar as C  # noqa: E402


def ano(d0, d1):
    return (d1 - d0).days / 365.25


def fluxo_independente(d, ev, base, R, inicio, cv=None):
    """Fluxo contratual por 1 de saldo, em termos do contrato (indexador cancela). Mesmas convenções do painel,
    código separado; na mesma data processa juros/PIK, depois prêmio, depois amortização/vencimento."""
    ordem = {"J": 0, "I": 0, "P": 1, "A": 2, "V": 3}
    evs = sorted(((datetime.strptime(e[0], "%d/%m/%Y").date(), e[1], float(e[2] or 0), e[3]) for e in ev),
                 key=lambda x: (x[0], ordem.get(x[1], 9)))
    evs = [e for e in evs if e[0] > d and e[1] in ordem]
    if not evs:
        return []
    cupons = sorted({e[0] for e in evs if e[1] in ("J", "I")})
    if len(cupons) >= 2:
        ini = cupons[0] - (cupons[1] - cupons[0])
    else:
        ini = inicio or d
    if inicio and ini < inicio:
        ini = inicio
    ini = min(ini, d)
    di = base == "DI+" and cv is not None
    curto = cv.pre(0.01) if di else 0.0

    def fdi(da, db):          # fator DI entre duas datas (passado aproximado pela taxa curta, futuro pela curva)
        if not di:
            return 1.0
        ta, tb = ano(d, da), ano(d, db)
        f_pass = (1 + curto) ** (-ta) if ta < 0 else 1.0
        return f_pass * cv.fator_di(tb) / (cv.fator_di(ta) if ta > 0 else 1.0)

    saldo, ref_j, ref_p, fluxo = 1.0, ini, ini, {}
    for dt, cod, taxa, bs in evs:
        cx = 0.0
        if cod == "J":
            cx = saldo * (fdi(ref_j, dt) * (1 + taxa / 100) ** ano(ref_j, dt) - 1)
            ref_j = dt
        elif cod == "I":
            saldo *= fdi(ref_j, dt) * (1 + taxa / 100) ** ano(ref_j, dt)
            ref_j = dt
        elif cod == "P":
            cx = saldo * ((1 + taxa / 100) ** ano(ref_p, dt) - 1) if taxa else 0.0
            ref_p = dt
        elif cod == "A":
            a = (taxa / 100) / R if bs == "FIX" else saldo * taxa / 100
            a = max(0.0, min(a, saldo))
            saldo -= a
            cx = a
        elif cod == "V":
            cx, saldo = saldo, 0.0
        if cx:
            t = ano(d, dt)
            fluxo[t] = fluxo.get(t, 0.0) + cx
    if saldo > 1e-9:
        t = ano(d, evs[-1][0])
        fluxo[t] = fluxo.get(t, 0.0) + saldo
    return sorted(fluxo.items())


def newton(cf, cupom, pct, cv=None):
    """Newton em s: PV(s)/PV(cupom) = % do par, com desconto 1/(fatorDI(t)·(1+s)^t) se houver curva (DI+)."""
    g = [(t, v / (cv.fator_di(t) if cv else 1.0)) for t, v in cf]
    pv = lambda s_: sum(w / (1 + s_) ** t for t, w in g)
    alvo = pct / 100 * pv(cupom)
    s = cupom
    for _ in range(200):
        f = pv(s) - alvo
        df = sum(-t * w / (1 + s) ** (t + 1) for t, w in g)
        if df == 0:
            return None
        s2 = s - f / df
        if s2 <= -0.95:
            s2 = (s - 0.95) / 2
        if abs(s2 - s) < 1e-12:
            return s2 * 100
        s = s2
    return None


def main():
    datas = sorted(p.stem for p in (C.DADOS / "daily").glob("*.json"))
    ds = datas[-1]
    d = date.fromisoformat(ds)
    dia = json.loads((C.DADOS / "daily" / f"{ds}.json").read_text(encoding="utf-8"))
    snd = C.snd_cadastro()
    agenda = C.snd_agenda()
    pu_snd = C.coletar_snd_pu(d)
    cj = C.obter_curva(d, baixar=False)
    cv = C.Curva(cj) if cj else None
    res, r1, r2, r3 = [], Counter(), Counter(), Counter()
    for a in dia["ativos"]:
        p = a.get("p") or {}
        if p.get("y") is None and not a.get("a"):
            continue
        c = a["c"]
        s, ev, anb = snd.get(c), agenda.get(c), a.get("a") or {}
        linha = {"c": c, "t": a.get("t"), "met": p.get("y_met"), "base": p.get("y_base"), "y": p.get("y")}
        # 1) reprodução com o motor do painel
        if p.get("y") is not None:
            novo = C.calcular_preco_e_yield(a.get("t"), d, a.get("n"), anb or None, s, pu_snd.get(c), cv, ev)
            linha["y1"] = novo.get("y")
            dif = None if novo.get("y") is None else abs(novo["y"] - p["y"])
            linha["d1"] = dif
            r1["igual (≤0,01 p.p.)" if dif is not None and dif <= 0.01 else "diferente" if dif is not None else "não reproduziu"] += 1
        # 2) implementação independente
        pct = None
        if p.get("pu_par"):
            preco = (a.get("n") or {}).get("pu_med") or anb.get("pu")
            pct = round(preco / p["pu_par"] * 100, 2) if preco else None
        if p.get("y") is not None and pct:
            if p.get("y_met") == "agenda" and s and ev:
                base = C.base_indexador(s.get("indice"), s.get("mult"))
                ini = C._data(s.get("inicio_rent")) or C._data(s.get("dt_emissao"))
                usa_di = base == "DI+" and cv is not None
                cf = fluxo_independente(d, ev, base, C.saldo_relativo(s), ini, cv if usa_di else None)
                cup = next((float(e[2]) / 100 for e in ev if e[1] == "J"), (s.get("taxa") or 0) / 100)
                y2 = newton(cf, cup, pct, cv if usa_di else None) if cf else None
                linha["y2"] = None if y2 is None else round(y2, 2)
                dif = None if y2 is None else abs(y2 - p["y"])
                linha["d2"] = dif
                r2["igual (≤0,05 p.p.)" if dif is not None and dif <= 0.05 else "diferente" if dif is not None else "sem solução"] += 1
            elif p.get("y_met") == "duration" and anb.get("tx_ind") is not None and anb.get("duration"):
                preco = (a.get("n") or {}).get("pu_med") or anb.get("pu")
                y2 = ((1 + anb["tx_ind"] / 100) * (anb["pu"] / preco) ** (252 / anb["duration"]) - 1) * 100
                dif = abs(y2 - p["y"]); linha.update(y2=round(y2, 2), d2=dif)
                r2["igual (≤0,05 p.p.)" if dif <= 0.05 else "diferente"] += 1
            elif p.get("y_met") == "bullet" and anb.get("vencimento") and anb.get("indice"):
                cup = C.taxa_do_texto(anb.get("indice"))
                T = ano(d, C._data(anb["vencimento"]))
                if cup is not None and T > 0:
                    y2 = ((1 + cup / 100) * (pct / 100) ** (-1 / T) - 1) * 100       # forma fechada
                    dif = abs(y2 - p["y"]); linha.update(y2=round(y2, 2), d2=dif)
                    r2["igual (≤0,05 p.p.)" if dif <= 0.05 else "diferente"] += 1
                else:
                    r2["bullet sem dados"] += 1
            else:
                r2[f"método {p.get('y_met')}: sem checagem independente"] += 1
        # 3) mercado: yield no PU ANBIMA com o fluxo do painel x taxa indicativa ANBIMA
        if anb.get("tx_ind") is not None and anb.get("pu") and anb.get("pu_par") and s and ev:
            base = C.base_indexador(s.get("indice"), s.get("mult"))
            if base in ("DI+", "IPCA+", "PRE"):
                pct_a = anb["pu"] / anb["pu_par"] * 100
                novo = C.calcular_preco_e_yield("DEB", d, None, {**anb, "pu_par": anb["pu_par"]}, s, None, cv, ev)
                ya = novo.get("y")
                if ya is not None and novo.get("y_met") == "agenda":
                    dif = ya - anb["tx_ind"]
                    linha.update(y3=ya, anb=anb["tx_ind"], d3=round(dif, 3), pct_anb=round(pct_a, 2))
                    ad = abs(dif)
                    r3["≤0,10 p.p." if ad <= 0.10 else "0,10–0,50 p.p." if ad <= 0.5 else "0,50–2 p.p." if ad <= 2 else ">2 p.p."] += 1
        res.append(linha)
    out = {"data": ds, "verif_1_reproducao": dict(r1), "verif_2_independente": dict(r2), "verif_3_anbima": dict(r3),
           "itens": res}
    C.escrever_json(C.DADOS / "_debug" / "verificacao_yield.json", out)
    print(json.dumps({k: v for k, v in out.items() if k != "itens"}, ensure_ascii=False, indent=1))
    for k, nome in (("d1", "reprodução"), ("d2", "independente")):
        piores = sorted((x for x in res if x.get(k) is not None), key=lambda x: -x[k])[:15]
        print(f"\npiores ({nome}):", [(x["c"], x["y"], x.get("y1" if k == "d1" else "y2"), round(x[k], 3)) for x in piores])
    piores = sorted((x for x in res if x.get("d3") is not None), key=lambda x: -abs(x["d3"]))[:30]
    print("\nDETALHE", json.dumps([x for x in res if (x.get("d1") or 0) > 0.01 or (x.get("d2") or 0) > 0.05 or abs(x.get("d3") or 0) > 0.5], ensure_ascii=False))
    print("\npiores (ANBIMA):", [(x["c"], x["base"], x["y3"], x["anb"], x["d3"], x["pct_anb"]) for x in piores])


if __name__ == "__main__":
    main()
