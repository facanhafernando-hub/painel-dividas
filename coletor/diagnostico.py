"""Diagnóstico: OCR da escritura da 6ª emissão da BCBF (PDF escaneado): fiança, remuneração, amortização."""
import glob, io, os, re, subprocess, time, zipfile, urllib.request
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def get(u, t=240):
    for i in range(5):
        try:
            return urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": UA}), timeout=t).read()
        except Exception as e:
            err = e; time.sleep(6 * (i + 1))
    raise err


z = zipfile.ZipFile(io.BytesIO(get("https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/IPE/DADOS/ipe_cia_aberta_2021.zip")))
txt = z.read(z.namelist()[0]).decode("latin-1").splitlines()
cab = txt[0].split(";")
url = next(dict(zip(cab, l.split(";")))["Link_Download"] for l in txt[1:]
           if "19.276.528/0001-16" in l and "Escritura da 6" in l)
b = get(url)
pdf = b if b[:4] == b"%PDF" else zipfile.ZipFile(io.BytesIO(b)).read([n for n in zipfile.ZipFile(io.BytesIO(b)).namelist() if n.lower().endswith(".pdf")][0])
open("/tmp/esc.pdf", "wb").write(pdf)
os.makedirs("/tmp/pg", exist_ok=True)
subprocess.run(["pdftoppm", "-r", "200", "-gray", "-png", "/tmp/esc.pdf", "/tmp/pg/p"], check=True)
paginas = sorted(glob.glob("/tmp/pg/p-*.png"))
print("páginas:", len(paginas))
texto = []
for n, p in enumerate(paginas, 1):
    t = subprocess.run(["tesseract", p, "-", "-l", "por", "--psm", "6"], capture_output=True).stdout.decode("utf-8", "replace")
    texto.append(f"\n=== PÁGINA {n} ===\n{t}")
full = "".join(texto)
open("/tmp/esc.txt", "w").write(full)
print("caracteres OCR:", len(full))
PAL = re.compile(r"fian[çc]a|fiador|fidejuss|benef[íi]cio de ordem|solid[áa]ri|principal pagador|sobretaxa|spread|1,45|1,60|amortiza[çc][ãa]o do valor nominal|data de vencimento|esp[ée]cie|garantia real", re.I)
linhas = full.splitlines()
vistos = set()
for i, l in enumerate(linhas):
    if l.startswith("=== PÁGINA"):
        pag = l
    if PAL.search(l) and i not in vistos:
        for j in range(i - 1, i + 4):
            vistos.add(j)
        print(f"[{pag[11:-4].strip()}]", re.sub(r"\s+", " ", " ".join(linhas[max(0, i - 1):i + 4]))[:520])
