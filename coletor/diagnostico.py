"""Diagnóstico: tipos de evento na agenda do SND (cronograma de todas as debêntures)."""
import collections, time, urllib.request, urllib.parse
from datetime import date, timedelta

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
S = "https://www.debentures.com.br/exploreosnd/consultaadados/eventosfinanceiros/agenda_e.asp"


def baixa(di, df):
    q = urllib.parse.quote
    u = f"{S}?op_exc=False&ativo=&dt_ini={q(di.strftime('%d/%m/%Y'))}&dt_fim={q(df.strftime('%d/%m/%Y'))}&evento="
    t0 = time.time()
    b = urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": UA}), timeout=180).read()
    linhas = [l.split("\t") for l in b.decode("cp1252", errors="replace").replace("\r", "").split("\n") if l.count("\t") >= 6]
    return linhas[1:], time.time() - t0, len(b)


hoje = date.today()
tot = []
for k in range(0, 8):
    di = hoje + timedelta(days=365 * k)
    df = di + timedelta(days=364)
    try:
        ls, dt, n = baixa(di, df)
        print(f"{di}..{df}: {len(ls)} eventos, {n/1e6:.1f} MB, {dt:.0f}s")
        tot += ls
    except Exception as e:
        print(di, "erro", e)
# janela longa de uma vez (o resto até 2080)
try:
    ls, dt, n = baixa(hoje + timedelta(days=365 * 8), date(2080, 12, 31))
    print(f"8a..2080: {len(ls)} eventos, {n/1e6:.1f} MB, {dt:.0f}s"); tot += ls
except Exception as e:
    print("longo erro", e)

ev = collections.Counter(r[4].strip() for r in tot)
print("\nEVENTOS:", ev.most_common())
for e in ev:
    tipos = collections.Counter(r[5].strip() for r in tot if r[4].strip() == e)
    print(f"\n[{e}] tipos:", tipos.most_common(12))
    amostra = [r for r in tot if r[4].strip() == e][:4]
    for r in amostra:
        print("    ", [x.strip() for x in r[:9]])
print("\nliquidação:", collections.Counter(r[7].strip() if len(r) > 7 else "" for r in tot).most_common(8))
ativos = {r[3].strip() for r in tot}
print("ativos com eventos futuros:", len(ativos))
am = collections.Counter(r[3].strip() for r in tot if r[4].strip().startswith("Amort"))
print("ativos com amortização:", len(am))
for cod in ("HAPV15", "HAPVA0", "RAIZ14", "LIGHB4", "RESA17"):
    print(f"\n{cod}:")
    for r in [x for x in tot if x[3].strip() == cod][:30]:
        print("    ", [x.strip() for x in r[:8]])
