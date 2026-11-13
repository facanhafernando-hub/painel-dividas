"""Diagnóstico: fontes de devedor de CRI/CRA (CVM dados abertos SECURIT, Fundos.NET/B3)."""
import io, json, re, zipfile, urllib.request, urllib.error
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def get(u, timeout=120, headers=None):
    h = {"User-Agent": UA, "Accept": "*/*"}; h.update(headers or {})
    try:
        r = urllib.request.urlopen(urllib.request.Request(u, headers=h), timeout=timeout)
        return r.status, r.headers.get("Content-Type"), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type"), e.read()
    except Exception as e:
        return None, None, repr(e).encode()


print("=== CVM dados abertos: SECURIT ===")
for u in ["https://dados.cvm.gov.br/dados/SECURIT/", "https://dados.cvm.gov.br/dados/SECURIT/DOC/",
          "https://dados.cvm.gov.br/dados/SECURIT/DOC/INF_MENSAL/DADOS/", "https://dados.cvm.gov.br/dados/SECURIT/CAD/DADOS/"]:
    st, ct, b = get(u)
    print(st, u, re.findall(r'href="([^"?/][^"]*)"', b.decode("latin-1"))[:40])

st, ct, b = get("https://dados.cvm.gov.br/dados/SECURIT/DOC/INF_MENSAL/DADOS/")
zips = re.findall(r'href="([^"]+\.zip)"', b.decode("latin-1"))
if zips:
    u = "https://dados.cvm.gov.br/dados/SECURIT/DOC/INF_MENSAL/DADOS/" + zips[-1]
    st, ct, b = get(u, 240)
    print("\nzip", u, st, len(b))
    if st == 200:
        z = zipfile.ZipFile(io.BytesIO(b))
        for n in z.namelist():
            txt = z.read(n).decode("latin-1").splitlines()
            print("\n--", n, len(txt), "linhas")
            print(txt[0][:1500])
            for l in txt[1:3]:
                print("   ", l[:700])

print("\n=== Fundos.NET (B3) ===")
for u in ["https://fnet.bmfbovespa.com.br/fnet/publico/pesquisarGerenciadorDocumentosDados?d=1&s=0&l=5&o%5B0%5D%5BdataEntrega%5D=desc&tipoFundo=8",
          "https://fnet.bmfbovespa.com.br/fnet/publico/pesquisarGerenciadorDocumentosDados?d=1&s=0&l=5&o%5B0%5D%5BdataEntrega%5D=desc&tipoFundo=11",
          "https://fnet.bmfbovespa.com.br/fnet/publico/listarFundos?term=opea"]:
    st, ct, b = get(u, headers={"X-Requested-With": "XMLHttpRequest", "Referer": "https://fnet.bmfbovespa.com.br/fnet/publico/abrirGerenciadorDocumentosCVM"})
    print(st, ct, u, "\n   ", b[:1500].decode("utf-8", "replace"))
