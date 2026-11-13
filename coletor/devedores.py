"""Devedor de cada CRI/CRA pelos informes mensais da CVM (dados abertos SECURIT).

  inf_mensal_{cri,cra}_classe:            código CETIP da série -> certificado (ISIN) e securitizadora
  inf_mensal_{cri,cra}_cedente_devedor:   certificado -> CNPJ do devedor/cedente e percentual do lastro

Regra: devedor com >= 50% do lastro (no informe mais recente); se não houver, cedente com >= 50%; senão "pulverizado".
O nome vem, nesta ordem: emissora de debêntures com o mesmo CNPJ (SND), cadastro de companhias abertas da CVM,
BrasilAPI (Receita), com cache em docs/data/cnpj_nomes.json.
Saída: docs/data/devedores_cricra.json  {codigo: {"cnpj", "nome", "pct", "tipo", "ref"}}  (tipo "pulverizado" sem cnpj)
"""
import csv, io, json, re, sys, time, urllib.error, urllib.request, zipfile
csv.field_size_limit(10**9)
from datetime import date
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
DADOS = RAIZ / "docs" / "data"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
dig = lambda s: re.sub(r"\D", "", s or "")


def log(*a):
    print(*a, flush=True)


def get(u, timeout=240, tentativas=4):
    for i in range(tentativas):
        try:
            return urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": UA}), timeout=timeout).read()
        except urllib.error.HTTPError as e:
            if e.code in (404, 400):
                raise
        except Exception:
            pass
        time.sleep(5 * (i + 1))
    raise RuntimeError(f"falhou: {u}")


def ler_csv(z, sufixo):
    nome = next((n for n in z.namelist() if n.endswith(sufixo)), None)
    if not nome:
        return []
    return list(csv.DictReader(io.StringIO(z.read(nome).decode("latin-1")), delimiter=";"))


def informes():
    classe, cd = [], []
    ano = date.today().year
    for tipo in ("cri", "cra"):
        for a in (ano - 1, ano):
            try:
                z = zipfile.ZipFile(io.BytesIO(get(f"https://dados.cvm.gov.br/dados/SECURIT/DOC/INF_MENSAL_{tipo.upper()}/DADOS/inf_mensal_{tipo}_{a}.zip")))
            except Exception as e:
                log("  sem informe", tipo, a, e)
                continue
            classe += ler_csv(z, f"_classe_{a}.csv")
            cd += ler_csv(z, f"_cedente_devedor_{a}.csv")
    log(f"linhas: classe {len(classe)} | cedente/devedor {len(cd)}")
    return classe, cd


def nomes_cnpj(cnpjs, cache):
    """Nome por CNPJ: SND (emissoras de debêntures), cadastro CVM, BrasilAPI. Atualiza o cache."""
    try:
        cat = json.load(open(DADOS / "catalogo.json"))["itens"]
        for i in cat:
            c = dig(i.get("cnpj"))
            if c and i["t"] == "DEB" and c not in cache:
                cache[c] = i["e"]
    except Exception:
        pass
    faltam = [c for c in cnpjs if c not in cache]
    if faltam:
        try:
            txt = get("https://dados.cvm.gov.br/dados/CIA_ABERTA/CAD/DADOS/cad_cia_aberta.csv").decode("latin-1")
            for r in csv.DictReader(io.StringIO(txt), delimiter=";"):
                c = dig(r.get("CNPJ_CIA"))
                if c and c not in cache and r.get("DENOM_SOCIAL"):
                    cache[c] = r["DENOM_SOCIAL"]
        except Exception as e:
            log("  cadastro CVM indisponível", e)
    faltam = [c for c in cnpjs if c not in cache]
    log(f"CNPJs sem nome após SND/CVM: {len(faltam)} (consultando BrasilAPI)")
    for n, c in enumerate(faltam):
        for u in (f"https://brasilapi.com.br/api/cnpj/v1/{c}", f"https://minhareceita.org/{c}"):
            try:
                j = json.loads(get(u, timeout=30, tentativas=2))
                if j.get("razao_social"):
                    cache[c] = j["razao_social"]
                    break
            except Exception:
                pass
        time.sleep(0.6)
        if n and n % 50 == 0:
            log(f"  {n}/{len(faltam)}")
    return cache


def main():
    classe, cd = informes()
    # certificado -> devedores do informe mais recente
    ult = {}
    for r in cd:
        k = r["Codigo_Identificacao_Certificado"]
        ref = (r["Data_Referencia"], int(r.get("Versao") or 0))
        if k not in ult or ref > ult[k][0]:
            ult[k] = (ref, [])
        if ult[k][0] == ref:
            try:
                p = float((r.get("Percentual") or "0").replace(",", "."))
            except ValueError:
                p = 0.0
            ult[k][1].append((r["Tipo"], dig(r.get("CNPJ")), p))
    escolha = {}
    for k, (ref, lst) in ult.items():
        pick = None
        for tipo in ("Devedor", "Cedente"):
            cands = sorted([x for x in lst if x[0] == tipo and x[1] and x[2] >= 0.5], key=lambda x: -x[2])
            if cands:
                pick = {"cnpj": cands[0][1], "pct": round(cands[0][2] * 100, 1), "tipo": tipo.lower(), "ref": ref[0]}
                break
        if not pick and lst:
            pick = {"cnpj": None, "pct": None, "tipo": "pulverizado", "ref": ref[0]}
        if pick:
            escolha[k] = pick
    # código CETIP -> certificado
    cod_cert = {}
    for r in classe:
        cod = (r.get("Codigo_CETIP") or "").strip().upper()
        if cod and r["Codigo_Identificacao_Certificado"] in escolha:
            ref = r["Data_Referencia"]
            if cod not in cod_cert or ref > cod_cert[cod][1]:
                cod_cert[cod] = (r["Codigo_Identificacao_Certificado"], ref)
    cache_p = DADOS / "cnpj_nomes.json"
    cache = json.load(open(cache_p)) if cache_p.exists() else {}
    cnpjs = sorted({escolha[c]["cnpj"] for c, _ in cod_cert.values() if escolha[c]["cnpj"]})
    cache = nomes_cnpj(cnpjs, cache)
    cache_p.write_text(json.dumps(dict(sorted(cache.items())), ensure_ascii=False, indent=0), encoding="utf-8")
    out = {}
    for cod, (cert, _) in cod_cert.items():
        e = dict(escolha[cert])
        if e["cnpj"]:
            e["nome"] = cache.get(e["cnpj"])
        out[cod] = e
    (DADOS / "devedores_cricra.json").write_text(json.dumps(dict(sorted(out.items())), ensure_ascii=False, indent=0), encoding="utf-8")
    from collections import Counter
    log("códigos:", len(out), Counter(v["tipo"] for v in out.values()), "| com nome:", sum(1 for v in out.values() if v.get("nome")))


if __name__ == "__main__":
    main()
