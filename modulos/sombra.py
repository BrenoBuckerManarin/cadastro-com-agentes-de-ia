"""Modo sombra (07/10): mede, por TIPO de operacao, quantas tabelas o usuario aprovou sem mudar nada.

E a evidencia para tirar o usuario da aprovacao com seguranca: classe aprovada de primeira muitas
vezes, sem falha, pode virar politica; classe que ele corrige continua com ele. So MEDE: nunca
bloqueia nem muda o que o lote grava.

Como mede: cada vez que a tabela e gerada, cada produto/sistema pronto guarda a "impressao" (hash)
do que o usuario ve na tabela e QUANDO. Correcao pedida por ele = impressao nova com mensagem dele
no meio (o Codex se corrigindo sozinho antes de mostrar nao conta). No aplicar, uma linha vai para
lotes/_sombra.jsonl com a classe, quantas versoes o usuario viu e o resultado.
"""
from __future__ import annotations

import hashlib
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ARQUIVO = "_sombra.jsonl"
CHAVE = {"tray": "tray", "any": "any_prep"}
GRAVADO = ("gravado_verificado", "gravado_fase_1")
PREFIXO_POLITICA = "POLITICA-AUTONOMIA"  # o mesmo de autonomia.PREFIXO (sem import circular)


def classe(item: dict[str, Any], sistema: str) -> str:
    """O tipo de operacao, do mais especifico ao mais geral."""
    tarefa = item.get("tarefa")
    if sistema == "tray":
        # 14/10: card "SKU - COR" num pai que NAO existe na Tray (familia de produtos) e produto
        # novo de verdade (rota variations: SEO, categoria, marca). Contava como "cor nova" e inflava a
        # evidencia da classe que mais perto estava de virar aprovacao automatica.
        if tarefa == "adicionar_variacoes" and (item.get("tray") or {}).get("rota") == "variations":
            return "tray_produto_novo"
        return {"kit": "tray_kit", "adicionar_variacoes": "tray_cor_nova"}.get(tarefa, "tray_produto_novo")
    anyp = item.get("any") or {}
    if anyp.get("somente_vinculo_tray"):
        return "any_vinculo_tray"
    if anyp.get("so_canais"):  # escopo-completo --so MERCADO_LIVRE (05/10: ML sumido da familia)
        return "any_canal_faltando"
    if tarefa == "adicionar_variacoes" and (item.get("tray") or {}).get("rota") == "variations":
        return "any_produto_novo"  # 20/10: produto novo com card de cor, como na Tray
    return "any_variacao_nova" if tarefa == "adicionar_variacoes" else "any_produto_novo"


def impressao(item: dict[str, Any], sistema: str) -> str:
    """Hash do que o usuario ve na tabela para este produto/sistema."""
    estado = item.get(CHAVE[sistema]) or {}
    visto = {"tabela": (estado.get("tabela") or "").strip(), "avisos": estado.get("avisos") or [],
             "titulos": estado.get("titulos_exatos") or [], "eans": estado.get("eans_gerados") or {}}
    return hashlib.sha256(json.dumps(visto, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def marcar_tabela(lote: dict[str, Any], prontos: dict[str, list[str]], agora: datetime | None = None) -> None:
    """Chamado quando a tabela e gerada. `prontos`: sku -> sistemas que entraram na tabela."""
    momento = (agora or datetime.now(timezone.utc)).isoformat()
    for item in lote["items"]:
        for sistema in prontos.get(item["sku"], []):
            s = item.setdefault("sombra", {}).setdefault(
                sistema, {"classe": classe(item, sistema), "versoes": [], "momentos": []})
            imp = impressao(item, sistema)
            if not s["versoes"] or s["versoes"][-1] != imp:
                momentos = s.get("momentos")
                if momentos is not None and len(momentos) == len(s["versoes"]):
                    s["momentos"] = (momentos + [momento])[-20:]
                else:  # estado de antes de 08/10, sem momentos: conta como antes
                    s.pop("momentos", None)
                s["versoes"] = (s["versoes"] + [imp])[-20:]


# 08/10 (<SKU>): o Codex refez a meta 3x para tirar um aviso de SEO ANTES de mostrar a tabela; o
# usuario viu so a 4a e aprovou de primeira, mas contava "4 versoes, corrigida". Correcao do usuario
# = versao nova com mensagem DELE no meio. Fonte: as sessoes do Codex (mesmas que o placar le).
SESSOES = Path.home() / ".codex" / "sessions"
# so o que NAO e fala dele; resposta pelos botoes (<send_user_message_question_reply>) e fala
NAO_E_O_USUARIO = ("<environment_context", "<external_codex_apps", "# AGENTS.md", "Automation:")


def mensagens_do_usuario(desde: datetime, ate: datetime, pasta: Path | None = None) -> list[datetime] | None:
    """Quando o usuario escreveu no chat do Codex entre `desde` e `ate`. None = nao deu para saber
    (sem a pasta, ou teste sem pasta propria: teste nunca le arquivo real)."""
    if pasta is None:
        if "unittest" in sys.modules:
            return None
        pasta = SESSOES
    if not pasta.exists():
        return None
    out = []
    dia = desde.astimezone(timezone.utc).date()
    while dia <= ate.astimezone(timezone.utc).date():
        for arq in (pasta / f"{dia:%Y}" / f"{dia:%m}" / f"{dia:%d}").glob("*.jsonl"):
            try:
                texto = arq.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for linha in texto.splitlines():
                if '"role":"user"' not in linha.replace(" ", ""):
                    continue
                try:
                    d = json.loads(linha)
                except ValueError:
                    continue
                p = d.get("payload") or {}
                if p.get("type") != "message" or p.get("role") != "user":
                    continue
                conteudo = p.get("content")
                fala = " ".join(c.get("text", "") for c in conteudo if isinstance(c, dict)) \
                    if isinstance(conteudo, list) else str(conteudo or "")
                if not fala.strip() or fala.lstrip().startswith(NAO_E_O_USUARIO):
                    continue
                try:
                    ts = datetime.fromisoformat(str(d.get("timestamp", "")).replace("Z", "+00:00"))
                except ValueError:
                    continue
                if desde < ts < ate:
                    out.append(ts)
        dia += timedelta(days=1)
    return sorted(out)


def versoes_vistas(s: dict[str, Any], mensagens: list[datetime] | None) -> int:
    """Quantas versoes o USUARIO viu: troca de versao so conta com mensagem dele entre as duas.
    Sem momentos ou sem as mensagens: conta todas, como antes (na duvida, nao e de primeira)."""
    versoes, momentos = s.get("versoes") or [], s.get("momentos") or []
    if mensagens is None or len(momentos) != len(versoes):
        return len(set(versoes))
    t = [datetime.fromisoformat(m) for m in momentos]
    return 1 + sum(1 for a, b in zip(t, t[1:]) if any(a < m < b for m in mensagens))


def registrar_aplicar(lote: dict[str, Any], results: dict[str, dict], quote: str, quando: str,
                      destino: Path, mensagens: list[datetime] | None | str = "sessoes") -> list[dict[str, Any]]:
    """Uma linha por produto/sistema aplicado. Gravado: a contagem zera (a fase seguinte, como o
    AnyMarket depois da Tray, e outra tabela). Falhou: a contagem segue para a nova tentativa.
    `mensagens`: horarios das falas do usuario; padrao = ler das sessoes do Codex."""
    linhas = []
    for item in lote["items"]:
        for sistema, r in (results.get(item["sku"]) or {}).items():
            s = (item.get("sombra") or {}).get(sistema)
            if not s or not s.get("versoes"):
                continue
            geradas = len(set(s["versoes"]))
            falas = mensagens
            if falas == "sessoes":
                falas = None
                if len(s.get("momentos") or []) == len(s["versoes"]) and len(s["versoes"]) > 1:
                    falas = mensagens_do_usuario(datetime.fromisoformat(s["momentos"][0]),
                                                 datetime.fromisoformat(s["momentos"][-1]))
            versoes = versoes_vistas(s, falas)
            linhas.append({"quando": quando, "lote": lote["nome"], "sku": item["sku"], "sistema": sistema,
                           "classe": s["classe"], "versoes": versoes, "de_primeira": versoes == 1,
                           **({"versoes_geradas": geradas} if geradas != versoes else {}),
                           "resultado": r.get("status"), "quote": quote,
                           **({"autonomo": True} if str(quote or "").startswith(PREFIXO_POLITICA) else {})})
            if r.get("status") in GRAVADO:
                item["sombra"].pop(sistema, None)
    if linhas:
        destino.parent.mkdir(parents=True, exist_ok=True)
        with destino.open("a", encoding="utf-8") as fh:
            for linha in linhas:
                fh.write(json.dumps(linha, ensure_ascii=False) + "\n")
    return linhas


def ler(caminho: Path) -> list[dict[str, Any]]:
    if not caminho.exists():
        return []
    out = []
    for l in caminho.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            out.append(json.loads(l))
        except ValueError:
            continue
    return out


def resumo(linhas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Por classe: aplicacoes gravadas, quantas de primeira, e falhas (outra evidencia de risco)."""
    por = defaultdict(lambda: {"gravados": 0, "de_primeira": 0, "falhas": 0})
    for l in linhas:
        r = por[l["classe"]]
        if l.get("autonomo"):
            # aplicado pela politica: ninguem olhou a tabela, entao nao e evidencia A FAVOR;
            # falha continua contando CONTRA (a politica nao se alimenta sozinha)
            r["falhas"] += l.get("resultado") not in GRAVADO
            continue
        if l.get("resultado") in GRAVADO:
            r["gravados"] += 1
            r["de_primeira"] += bool(l.get("de_primeira"))
        else:
            r["falhas"] += 1
    return [{"classe": k, **v, "pct_de_primeira": round(100 * v["de_primeira"] / v["gravados"]) if v["gravados"] else 0}
            for k, v in sorted(por.items(), key=lambda kv: -kv[1]["gravados"])]


def tabela_md(linhas: list[dict[str, Any]]) -> str:
    rows = resumo(linhas)
    if not rows:
        return "Sem aplicacoes registradas ainda (o modo sombra mede a partir de 07/10).\n"
    md = ["| Classe | Gravados | Aprovados de primeira | % | Falhas |", "|---|---:|---:|---:|---:|"]
    md += [f"| {r['classe']} | {r['gravados']} | {r['de_primeira']} | {r['pct_de_primeira']}% | {r['falhas']} |"
           for r in rows]
    return "\n".join(md) + "\n"
