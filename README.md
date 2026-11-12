# Painel de Dívidas · Mercado Secundário

Acompanhamento diário de debêntures, CRI, CRA e afins negociados no balcão da B3, com PU negociado, volume, número de negócios e PU/taxa indicativos da ANBIMA.

## Como funciona

- `coletor/coletar.py` baixa, em dias úteis à noite:
  - **B3**: Boletim Diário, tabela "Negociação consolidada" (todos os negócios de renda fixa do dia);
  - **ANBIMA**: Mercado Secundário de Debêntures (`dbAAMMDD.txt`).
- Junta tudo por código do ativo, calcula alertas e grava em `docs/data/`.
- `.github/workflows/coletar.yml` roda o coletor às 20:47 e às 23:17 (Brasília) e salva os dados no repositório.
- `docs/index.html` é o site (GitHub Pages), que lê os JSON.

## Alertas

| Alerta | Regra |
|---|---|
| Vencida antecip. | ANBIMA lista na categoria de vencidos antecipadamente |
| Negociado < 80% par | PU médio negociado / PU par (ANBIMA) < 80% |
| Indicativo < 80% par | sem negócio no dia e % PU par indicativo < 80% |
| Desvio vs ANBIMA | PU negociado difere do indicativo em mais de 2% |
| Salto de volume | volume ≥ R$ 1 mi e > 3x a média dos últimos 20 pregões |

Limites ajustáveis no topo de `coletor/coletar.py`.

## Rodar manualmente

Aba **Actions** → "Coletar dados" → **Run workflow**. Campo `backfill` reprocessa dias úteis anteriores (a B3 só guarda ~21 dias úteis).

Local: `python coletor/coletar.py --data 2026-11-12 --backfill 20`

## Diagnóstico

Na primeira coleta, `docs/data/_debug/` guarda as colunas cruas da B3 e as primeiras linhas do arquivo da ANBIMA. Se alguma coluna não for reconhecida, o log da Action mostra `ATENÇÃO: colunas B3 não mapeadas`.
