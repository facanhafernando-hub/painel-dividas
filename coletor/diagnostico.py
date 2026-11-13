"""Diagnóstico: base dos percentuais de amortização (VNE x VNA, FIX x VAR) na agenda do SND."""
import collections, time, urllib.request, urllib.parse
from datetime import date, timedelta

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
S = "https://www.debentures.com.br/exploreosnd/consultaadados/eventosfinanceiros/agenda_e.asp"


def agenda(ativo, di=date(2000, 1, 1), df=date(2080, 12, 31)):
    q = urllib.parse.quote
    u = f"{S}?op_exc=False&ativo={ativo}&dt_ini={q(di.strftime('%d/%m/%Y'))}&dt_fim={q(df.strftime('%d/%m/%Y'))}&evento="
    b = urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": UA}), timeout=120).read()
    return [[x.strip() for x in l.split("\t")] for l in b.decode("cp1252", errors="replace").replace("\r", "").split("\n") if l.count("\t") >= 6][1:]


def num(s):
    try:
        return float(s.replace(".", "").replace(",", "."))
    except Exception:
        return 0.0


# pega exemplos de cada base a partir da agenda do próximo ano
hoje = date.today()
q = urllib.parse.quote
u = f"{S}?op_exc=False&ativo=&dt_ini={q(hoje.strftime('%d/%m/%Y'))}&dt_fim={q((hoje + timedelta(days=365)).strftime('%d/%m/%Y'))}&evento="
b = urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": UA}), timeout=120).read()
ls = [[x.strip() for x in l.split("\t")] for l in b.decode("cp1252", errors="replace").replace("\r", "").split("\n") if l.count("\t") >= 6][1:]
por_tipo = collections.defaultdict(list)
for r in ls:
    if r[4].startswith("Amort"):
        if r[3] not in por_tipo[r[5]]:
            por_tipo[r[5]].append(r[3])
for tipo, ativos in por_tipo.items():
    print("=" * 70, f"\nTIPO {tipo!r}: {len(ativos)} ativos")
    for a in ativos[:4]:
        ev = agenda(a)
        am = [r for r in ev if r[4].startswith("Amort")]
        venc = [r for r in ev if r[4] == "Vencimento"]
        ps = [num(r[6]) for r in am]
        passado = [num(r[6]) for r in am if r[0][-4:] + r[0][3:5] + r[0][:2] <= hoje.strftime("%Y%m%d")]
        print(f"  {a}: {len(am)} amortizações (passadas {len(passado)}), soma % total {sum(ps):.2f}, soma passada {sum(passado):.2f}; "
              f"primeira {am[0][0] if am else '-'}, última {am[-1][0] if am else '-'}, venc {venc[0][0] if venc else '-'}")
        print("     %:", [round(p, 4) for p in ps[:14]], "..." if len(ps) > 14 else "")
