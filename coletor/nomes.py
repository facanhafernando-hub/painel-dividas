"""Nome pelo qual cada empresa é conhecida no mercado (ex.: "HAPVIDA PARTICIPACOES E INVESTIMENTOS S.A." -> "Hapvida").

Lê as empresas do catálogo, pede ao modelo os nomes em blocos (só as que ainda não têm nome), resolve nomes repetidos
entre empresas diferentes (subsidiárias, distribuidoras regionais, SPEs) e grava docs/data/nomes_empresas.json:
  {"<chave>": {"nome": "Hapvida", "razao": "<razão social>", "fonte": "modelo" | "manual"}}
Entradas com fonte "manual" nunca são sobrescritas.
"""
import collections, json, os, re, sys, time, urllib.error, urllib.request
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
DADOS = RAIZ / "docs" / "data"
ARQ = DADOS / "nomes_empresas.json"
CHAVE = re.sub(r"\s", "", os.environ.get("ANTHROPIC_API_KEY", ""))
MODELO = "claude-sonnet-5-5"

REGRAS = """Você conhece o mercado de crédito privado brasileiro. Para cada razão social, devolva o nome curto pelo qual a empresa é conhecida no mercado, como apareceria numa tela de terminal ou numa matéria do Valor.

Regras:
- Tire forma jurídica e palavras genéricas: S.A., S/A, Ltda, Companhia, Cia, Participações, Holding, Investimentos, Empreendimentos, "em recuperação judicial", etc. Ex.: "HAPVIDA PARTICIPACOES E INVESTIMENTOS S.A." -> "Hapvida"; "LOCALIZA RENT A CAR S.A." -> "Localiza".
- Quando a empresa é conhecida por uma marca ou nome de pregão diferente da razão social, use a marca. Ex.: "DIAGNOSTICOS DA AMERICA S.A." -> "Dasa"; "AGROPECUARIA MAGGI" -> "Amaggi"; "ARMAZEM MATEUS" -> "Grupo Mateus". Nunca troque uma empresa por outra parecida; se não tiver certeza da marca, use a razão social limpa.
- Use a marca conhecida quando a razão social é diferente dela: "COMPANHIA BRASILEIRA DE DISTRIBUICAO" -> "GPA"; "CM HOSPITALAR S.A." -> "Viveo (CM Hospitalar)"; "VIA S.A." -> "Casas Bahia (Via)".
- Subsidiárias, distribuidoras regionais, concessionárias e SPEs do mesmo grupo precisam continuar distintas: inclua o qualificador que diferencia. Ex.: "EQUATORIAL GOIAS DISTRIBUIDORA DE ENERGIA S.A." -> "Equatorial Goiás"; "RAIZEN ENERGIA S.A." -> "Raízen Energia"; "RAIZEN S.A." -> "Raízen"; "LIGHT SERVICOS DE ELETRICIDADE S.A." -> "Light SESA"; "ENERGISA MATO GROSSO - DISTRIBUIDORA DE ENERGIA S.A." -> "Energisa MT"; "CONCESSIONARIA ROTA DAS BANDEIRAS S.A." -> "Rota das Bandeiras".
- Securitizadoras: nome curto da securitizadora (ex.: "Opea Securitizadora", "Riza Securitizadora", "Virgo").
- Siglas consagradas ficam em sigla: "COMPANHIA DE SANEAMENTO BASICO DO ESTADO DE SAO PAULO" -> "Sabesp"; "CENTRAIS ELETRICAS BRASILEIRAS" -> "Eletrobras".
- Grafia correta em português, com acentos e caixa normal (não tudo maiúsculo), exceto siglas.
- Se não reconhecer a empresa, apenas limpe a razão social pelas regras acima (não invente marca).

Responda APENAS com JSON: {"<id>": "<nome curto>", ...} usando os ids recebidos."""

DESEMPATE = """Os grupos abaixo são empresas DIFERENTES (CNPJs diferentes) que receberam o mesmo nome curto. Para cada id, devolva um nome curto que diferencie as empresas do grupo entre si, mantendo a marca e acrescentando o qualificador da razão social (região, segmento, "Holding", sigla da SPE etc.). Responda APENAS com JSON: {"<id>": "<nome>"}."""


def log(*a):
    print(*a, flush=True)


def api(corpo):
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=json.dumps(corpo).encode(),
                                 headers={"x-api-key": CHAVE, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    for i in range(6):
        try:
            r = json.loads(urllib.request.urlopen(req, timeout=600).read())
            return "".join(b.get("text", "") for b in r["content"]), r.get("usage", {})
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 529):
                time.sleep(20 * (i + 1)); continue
            raise RuntimeError(f"API {e.code}: {e.read()[:300]}")
    raise RuntimeError("API indisponível")


def ler_json(txt):
    m = re.search(r"\{.*\}", txt, re.S)
    return json.loads(m.group(0)) if m else {}


def custo(u):
    return (u.get("input_tokens", 0) * 2 + u.get("output_tokens", 0) * 10) / 1e6


def main():
    if not CHAVE:
        sys.exit("ANTHROPIC_API_KEY ausente")
    forcar = "--forcar" in sys.argv
    cat = json.load(open(DADOS / "catalogo.json"))["itens"]
    emp = {}
    for i in cat:
        k = i.get("k")
        if not k:
            continue
        e = emp.setdefault(k, {"nomes": collections.Counter(), "cnpj": i.get("cnpj"), "tipos": set()})
        e["nomes"][i.get("e") or k] += 1
        e["tipos"].add(i["t"])
    mapa = json.load(open(ARQ)) if ARQ.exists() else {}
    fila = [k for k in sorted(emp) if forcar and mapa.get(k, {}).get("fonte") != "manual" or k not in mapa]
    log(f"empresas: {len(emp)} | sem nome: {len(fila)}")
    gasto = 0.0
    for n0 in range(0, len(fila), 90):
        bloco = fila[n0:n0 + 90]
        linhas = [f'{n}: {emp[k]["nomes"].most_common(1)[0][0]} ({"/".join(sorted(emp[k]["tipos"]))})' for n, k in enumerate(bloco)]
        txt, u = api({"model": MODELO, "max_tokens": 6000, "system": REGRAS,
                      "messages": [{"role": "user", "content": "\n".join(linhas)}]})
        gasto += custo(u)
        try:
            out = ler_json(txt)
        except Exception:
            out = {}
        for n, k in enumerate(bloco):
            nome = (out.get(str(n)) or "").strip()
            if nome:
                mapa[k] = {"nome": nome, "razao": emp[k]["nomes"].most_common(1)[0][0], "fonte": "modelo"}
        log(f"  bloco {n0 // 90 + 1}: {len(out)} nomes")
    # desempate: mesmo nome para empresas (chaves) diferentes
    por_nome = collections.defaultdict(list)
    for k, v in mapa.items():
        if k in emp:
            por_nome[v["nome"].lower()].append(k)
    grupos = [ks for ks in por_nome.values() if len(ks) > 1]
    log(f"nomes repetidos: {len(grupos)} grupos")
    if grupos:
        ids, linhas = {}, []
        for g in grupos:
            linhas.append("Grupo:")
            for k in g:
                ids[str(len(ids))] = k
                linhas.append(f"  {len(ids) - 1}: {mapa[k]['razao']} (CNPJ {emp[k]['cnpj'] or '?'})")
        txt, u = api({"model": MODELO, "max_tokens": 6000, "system": DESEMPATE,
                      "messages": [{"role": "user", "content": "\n".join(linhas)}]})
        gasto += custo(u)
        try:
            out = ler_json(txt)
        except Exception:
            out = {}
        for i_, k in ids.items():
            if out.get(i_) and mapa[k].get("fonte") != "manual":
                mapa[k]["nome"] = out[i_].strip()
    ARQ.write_text(json.dumps(dict(sorted(mapa.items())), ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"gravado: {len(mapa)} nomes | US$ {gasto:.3f}")


if __name__ == "__main__":
    main()
