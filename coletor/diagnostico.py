"""Diagnóstico: testa a chave da API (sem imprimir a chave)."""
import json, os, urllib.request, urllib.error
import re
k0 = os.environ.get("ANTHROPIC_API_KEY", "")
k = re.sub(r"\s", "", k0)
print("tinha espaços/quebras:", k0 != k, "| tamanho limpo:", len(k))
print("presente:", bool(k), "| prefixo ok:", k.startswith("sk-ant-"))
if k:
    for modelo in ("claude-haiku-5-5", "claude-sonnet-5-5"):
        body = json.dumps({"model": modelo, "max_tokens": 5, "messages": [{"role": "user", "content": "Responda só: ok"}]}).encode()
        req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, headers={
            "x-api-key": k, "anthropic-version": "2023-06-01", "content-type": "application/json"})
        try:
            r = urllib.request.urlopen(req, timeout=60); print(modelo, r.status, r.read()[:200])
        except urllib.error.HTTPError as e:
            print(modelo, e.code, e.read()[:300])
