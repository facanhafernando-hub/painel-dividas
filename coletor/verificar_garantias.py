"""Checa se cada garantia extraída está ancorada no texto dos documentos (sem API).

Para cada garantia: o CNPJ citado aparece no texto? As palavras significativas do fiador/objeto aparecem?
Saída: docs/data/_debug/verificacao_garantias.json + resumo no log.
"""
import json, re, sys, collections
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import escrituras as E  # noqa: E402

PARE = set("""S A SA LTDA DE DA DO DAS DOS E EM PARA COM SEU SUA SEUS SUAS ACOES ACAO QUOTAS COTAS EMISSAO TITULARIDADE
DIREITOS CREDITORIOS RECEBIVEIS CONTAS CONTA IMOVEIS IMOVEL BENS PROPRIEDADE TOTALIDADE CAPITAL SOCIAL ORDINARIAS
PREFERENCIAIS DECORRENTES CONTRATO CONTRATOS ALIENACAO CESSAO FIDUCIARIA FIANCA GARANTIA GARANTIAS EMISSORA FIADORA
COMPANHIA PARTICIPACOES OBJETO NAO DETALHADO TRECHOS CNPJ VINCULADA VINCULADAS FUNDO RESERVA DETERMINADAS""".split())
norm = lambda s: re.sub(r"[^A-Z0-9 ]", " ", E.sem_acento(s or "").upper())


def main():
    resultado, stat = [], collections.Counter()
    for p in sorted(E.SAIDA.glob("[0-9]*_*.json")):
        j = json.load(open(p))
        gm = j.get("garantias_meta") or {}
        if not j.get("garantias"):
            continue
        texto = ""
        for d in gm.get("documentos", []):
            try:
                texto += "\n" + E.texto_documento(d)
            except Exception:
                pass
        T, D = norm(texto), re.sub(r"\D", "", texto)
        for cod, g in j["garantias"].items():
            for x in g.get("garantias") or []:
                alvo = x.get("prestador_ou_objeto") or ""
                cnpjs = [re.sub(r"\D", "", c) for c in re.findall(r"\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}", alvo)]
                pal = [w for w in norm(alvo).split() if len(w) >= 4 and w not in PARE and not w.isdigit()]
                ok_cnpj = all(c in D for c in cnpjs) if cnpjs else None
                frac = (sum(1 for w in pal if re.search(r"\b" + re.escape(w) + r"\b", T)) / len(pal)) if pal else None
                if not texto.strip():
                    st = "sem texto"
                elif ok_cnpj is False:
                    st = "CNPJ não encontrado"
                elif frac is None:
                    st = "genérico (sem nome para checar)"
                elif frac >= 0.6 or ok_cnpj:
                    st = "ancorado"
                else:
                    st = "nome não encontrado"
                stat[st] += 1
                resultado.append({"c": cod, "tipo": x.get("tipo"), "alvo": alvo[:140], "situacao": x.get("situacao"),
                                  "st": st, "frac": None if frac is None else round(frac, 2)})
    E.SAIDA.parent.joinpath("_debug").mkdir(exist_ok=True)
    json.dump({"resumo": stat, "itens": resultado}, open(E.SAIDA.parent / "_debug" / "verificacao_garantias.json", "w"),
              ensure_ascii=False, indent=1)
    print(dict(stat))
    for r in resultado:
        if r["st"] not in ("ancorado", "genérico (sem nome para checar)"):
            print(" ", r["st"], "|", r["c"], "|", r["tipo"], "|", r["alvo"][:110], "|", r["frac"])


if __name__ == "__main__":
    main()
