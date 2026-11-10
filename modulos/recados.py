"""Recados do Claude (analista) para o Codex (08/10, proposta 1: o usuario deixa de ser mensageiro).

Antes o usuario copiava o que o Claude escrevia e colava no Codex ("O Claude corrigiu...", "continua o
lote"). Agora o Claude grava o recado aqui; o ciclo (a cada 45 min) poe os pendentes no AVISO e o
Codex faz e marca como feito, com o resultado.

Recado NUNCA aprova gravacao: aprovar e so com a frase do usuario. Frase de aprovacao que e trecho de
um recado e recusada (`eh_trecho_de_recado`).
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

ARQUIVO = "_recados.json"
TRECHO_MINIMO = 20  # "aprovo"/"pode aplicar" do usuario nunca sao barrados por aparecerem num recado


def carregar(path: Path) -> list[dict[str, Any]]:
    try:
        dados = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return dados if isinstance(dados, list) else []


def _salvar(path: Path, recados: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(recados, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def adicionar(path: Path, texto: str, de: str = "claude", quando: str | None = None) -> dict[str, Any]:
    texto = " ".join(str(texto or "").split())
    if not texto:
        raise ValueError("recado vazio")
    recados = carregar(path)
    numero = 1 + max((int(str(r.get("id", "R0"))[1:] or 0) for r in recados), default=0)
    novo = {"id": f"R{numero}", "quando": quando or time.strftime("%Y-%m-%d %H:%M"), "de": de,
            "texto": texto, "status": "pendente"}
    _salvar(path, recados + [novo])
    return novo


def pendentes(path: Path) -> list[dict[str, Any]]:
    return [r for r in carregar(path) if r.get("status") == "pendente"]


def concluir(path: Path, rid: str, resultado: str, quando: str | None = None) -> dict[str, Any]:
    resultado = " ".join(str(resultado or "").split())
    if not resultado:
        raise ValueError("diga o resultado: o que foi feito (ou por que nao deu)")
    recados = carregar(path)
    for r in recados:
        if r.get("id") == rid:
            if r.get("status") != "pendente":
                raise ValueError(f"{rid} ja esta {r.get('status')}")
            r.update(status="feito", feito_em=quando or time.strftime("%Y-%m-%d %H:%M"), resultado=resultado)
            _salvar(path, recados)
            return r
    raise ValueError(f"recado {rid} nao existe")


def feitos_desde(path: Path, desde: str) -> list[dict[str, Any]]:
    """Feitos a partir de `desde` ('AAAA-MM-DD HH:MM'): o AVISO mostra o resultado por um dia."""
    return [r for r in carregar(path) if r.get("status") == "feito" and str(r.get("feito_em", "")) >= desde]


def _normal(texto: str) -> str:
    return " ".join(str(texto or "").casefold().split())


def eh_trecho_de_recado(frase: str, path: Path) -> bool:
    """A frase de aprovacao veio do recado, nao do usuario."""
    frase = _normal(frase)
    if len(frase) < TRECHO_MINIMO:
        return False
    return any(frase in _normal(r.get("texto", "")) for r in carregar(path))
