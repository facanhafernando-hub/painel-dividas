"""Diagnóstico: onde estão os aditamentos da Viveo (CM Hospitalar) no IPE."""
import io, re, zipfile, urllib.request
UA = "Mozilla/5.0"
for ano in (2022, 2023, 2024, 2025, 2026):
    b = urllib.request.urlopen(urllib.request.Request(f"https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/IPE/DADOS/ipe_cia_aberta_{ano}.zip", headers={"User-Agent": UA}), timeout=240).read()
    z = zipfile.ZipFile(io.BytesIO(b)); txt = z.read(z.namelist()[0]).decode("latin-1").splitlines()
    cab = txt[0].split(";")
    for l in txt[1:]:
        r = dict(zip(cab, l.split(";")))
        if r.get("CNPJ_Companhia", "").replace(".", "").replace("/", "").replace("-", "") in ("12420164000157",) or "CREMER" in r.get("Nome_Companhia", "").upper():
            if re.search(r"escritura|aditamento|deb[eê]nt|vencimento", " ".join([r.get("Categoria",""), r.get("Tipo",""), r.get("Assunto","")]), re.I):
                print(r["Data_Entrega"][:10], "|", r["Nome_Companhia"][:20], "|", r["Categoria"][:45], "|", r.get("Tipo","")[:25], "|", r.get("Especie","")[:20], "|", r.get("Assunto","")[:140])
