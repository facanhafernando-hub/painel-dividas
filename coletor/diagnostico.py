"""Diagnóstico: estrutura dos informes mensais de CRI/CRA na CVM (devedor por emissão?)."""
import io, re, zipfile, urllib.request
UA = "Mozilla/5.0"
def get(u, t=240):
    return urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": UA}), timeout=t).read()
for tipo in ("INF_MENSAL_CRI", "INF_MENSAL_CRA"):
    base = f"https://dados.cvm.gov.br/dados/SECURIT/DOC/{tipo}/DADOS/"
    zips = re.findall(r'href="([^"]+\.zip)"', get(base).decode("latin-1"))
    print("==", tipo, zips[-4:])
    z = zipfile.ZipFile(io.BytesIO(get(base + zips[-1])))
    for n in z.namelist():
        txt = z.read(n).decode("latin-1").splitlines()
        print("\n--", n, len(txt), "linhas")
        print("   ", txt[0][:2500])
        for l in txt[1:3]:
            print("      ", l[:900])
