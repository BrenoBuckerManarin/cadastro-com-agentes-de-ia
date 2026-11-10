"""Aprovacao por politica (07/10) -- nasce DESLIGADA.

O usuario aprova UMA vez uma politica ("vinculo Tray com preco da planilha pode ser aplicado sem
me perguntar") e o ciclo aplica sozinho o lote que for INTEIRO dessas classes. Travas:
  - so liga classe com evidencia do modo sombra (sombra.py): gravados, % aprovado de primeira,
    zero falhas -- autonomia por dado, nao por palpite;
  - qualquer coisa que pede olho humano devolve o lote ao usuario: aviso de SEO/Any, EAN gerado,
    categoria, decisao tirada do historico, tentativa que ja falhou;
  - frase de politica sem politica valida e recusada no aplicar (o agente nao forja);
  - tudo que foi feito sozinho vai para lotes/_autonomia.jsonl e para o AVISO do dia;
  - `lote.py autonomia --desligar` corta na hora.
Nunca: ERP, Trello, publicar anuncio, apagar.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import sombra

AQUI = Path(__file__).resolve().parent
POLITICA = AQUI / "politica-autonomia.json"
REGISTRO = "_autonomia.jsonl"
PREFIXO = "POLITICA-AUTONOMIA"
EVIDENCIA_MINIMA = {"gravados": 10, "pct_de_primeira": 95, "falhas": 0}


def carregar(path: Path | None = None) -> dict[str, Any]:
    path = path or POLITICA
    base = {"ligada": False, "classes": [], "frase": "", "aprovada_em": "", "evidencia_minima": EVIDENCIA_MINIMA}
    if not path.exists():
        return base
    try:
        return {**base, **json.loads(path.read_text(encoding="utf-8"))}
    except ValueError:
        return base  # politica ilegivel = desligada


def salvar(politica: dict[str, Any], path: Path | None = None) -> None:
    (path or POLITICA).write_text(json.dumps(politica, ensure_ascii=False, indent=2), encoding="utf-8")


def evidencia(linhas_sombra: list[dict[str, Any]], minimo: dict[str, int] | None = None) -> dict[str, dict]:
    """Por classe: os numeros do modo sombra e se passam do minimo."""
    minimo = {**EVIDENCIA_MINIMA, **(minimo or {})}
    out = {}
    for r in sombra.resumo(linhas_sombra):
        faltas = []
        if r["gravados"] < minimo["gravados"]:
            faltas.append(f"{r['gravados']} gravados (minimo {minimo['gravados']})")
        if r["pct_de_primeira"] < minimo["pct_de_primeira"]:
            faltas.append(f"{r['pct_de_primeira']}% de primeira (minimo {minimo['pct_de_primeira']}%)")
        if r["falhas"] > minimo["falhas"]:
            faltas.append(f"{r['falhas']} falhas (maximo {minimo['falhas']})")
        out[r["classe"]] = {**r, "qualifica": not faltas, "faltas": faltas}
    return out


def _pede_olho_humano(item: dict[str, Any], sistema: str) -> list[str]:
    motivos = []
    if (item.get("erp") or {}).get("pendencias"):
        # 14/10: a Temp mostrou algo a acertar no ERP (Referencia, nome sem cor, medida...): o usuario ve antes
        motivos.append("pendencias de ERP lidas da Temp")
    if sistema == "tray":
        tray = item.get("tray") or {}
        if tray.get("avisos"):
            motivos.append("avisos de SEO na tabela")
        if tray.get("eans_gerados"):
            motivos.append("EAN gerado (o usuario precisa lancar no ERP)")
        if tray.get("previa_categoria"):
            motivos.append("categoria escolhida nesta rodada")
    else:
        if (item.get("any_prep") or {}).get("avisos"):
            motivos.append("avisos do AnyMarket na tabela")
        if (item.get("any") or {}).get("canais_do_historico"):
            motivos.append("canal novo decidido pelo historico")
    return motivos


def avaliar(lote: dict[str, Any], tudo_pronto: bool, politica: dict[str, Any],
            linhas_sombra: list[dict[str, Any]]) -> dict[str, Any]:
    """Pode o ciclo aplicar este lote sozinho? Devolve os motivos quando nao pode."""
    motivos: list[str] = []
    if not politica.get("ligada"):
        return {"pode": False, "motivos": ["politica desligada"], "itens": []}
    if not str(politica.get("frase") or "").strip():
        motivos.append("politica sem a frase de aprovacao do usuario")
    if not tudo_pronto or not lote.get("tabela_hash"):
        motivos.append("lote nao esta pronto")
    if lote.get("aplicado"):
        motivos.append("lote ja aplicado")
    if any(t.get("falhas") for t in lote.get("tentativas") or []):
        motivos.append("ja houve tentativa com falha: tentar de novo e com o usuario")
    ev = evidencia(linhas_sombra, politica.get("evidencia_minima"))
    ligadas = set(politica.get("classes") or [])
    itens = []
    for item in lote.get("items") or []:
        for sistema, chave in (("tray", "tray"), ("any", "any_prep")):
            if sistema not in item.get("sistemas", []) or (item.get(chave) or {}).get("status") != "pronto":
                continue
            c = sombra.classe(item, sistema)
            itens.append({"sku": item["sku"], "sistema": sistema, "classe": c})
            if c not in ligadas:
                motivos.append(f"{item['sku']} {sistema}: classe {c} fora da politica")
            elif not (ev.get(c) or {}).get("qualifica"):
                motivos.append(f"{item['sku']} {sistema}: classe {c} sem evidencia suficiente")
            motivos += [f"{item['sku']} {sistema}: {m}" for m in _pede_olho_humano(item, sistema)]
    if not itens:
        motivos.append("nada pronto para aplicar")
    return {"pode": not motivos, "motivos": motivos, "itens": itens}


def frase(politica: dict[str, Any], quando: str | None = None) -> str:
    """A --user-quote do aplicar feito pela politica: a frase do usuario, com data, e a rodada."""
    return (f"{PREFIXO} aprovada em {politica.get('aprovada_em')}: \"{politica.get('frase')}\" "
            f"(ciclo {quando or time.strftime('%Y-%m-%d %H:%M')})")


def eh_politica(quote: str) -> bool:
    return str(quote or "").strip().startswith(PREFIXO)


def registrar(destino: Path, linha: dict[str, Any]) -> None:
    destino.parent.mkdir(parents=True, exist_ok=True)
    with destino.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(linha, ensure_ascii=False) + "\n")


def feitos_hoje(destino: Path, hoje: str | None = None) -> list[dict[str, Any]]:
    hoje = hoje or time.strftime("%Y-%m-%d")
    return [l for l in sombra.ler(destino) if str(l.get("quando", "")).startswith(hoje)]
