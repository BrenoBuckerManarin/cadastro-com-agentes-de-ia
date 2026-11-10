"""Fila do que depende do usuario, com codigo fixo (14/10, medicao das rodadas de 13 e 14/10).

O AVISO era so para o agente: a pergunta vivia no chat e na memoria livre da rodada, e a rodada seguinte
nao sabia o que o usuario ja tinha respondido. Medido: a <SKU> teve "deixe a Tray de fora" as 08:19 e as
rodadas das 10:02 e 11:02 pediram marca/categoria/fotos da Tray de novo; o Material da <SKU> foi pedido
em 7 rodadas (dependia de outra pessoa); a frase do <SKU> em 16 rodadas, ~25 h; o genero da Netshoes em mais 3
rodadas depois do "Sim, Unissex" (era bloqueio do script, e o AVISO dizia PRONTO).

Aqui cada pendencia do usuario ganha um codigo (P1, P2...) que nao muda entre rodadas. O usuario responde
pelo codigo ("P3 total", "P3 nao precisa"), a resposta fica gravada e a pergunta nao volta.

Tipos: frase (pronto para a frase de aprovacao), tarefa (ERP, Temp, planilha, AnyMarket), decisao
(pergunta), parado (nao depende do usuario). Estados: aberto; respondido (resposta gravada, o agente
aplica); dispensado ("nao precisa"); resolvido (sumiu da fonte ou o lote fechou).
"""
from __future__ import annotations

import json
import re
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

ARQUIVO = "_fila.json"
GUARDAR_DIAS = 7  # resolvido sai do arquivo depois disso; o codigo nunca e reusado
TIPOS = ("frase", "tarefa", "decisao", "parado")
TITULOS = {"frase": "PRONTOS PARA SUA FRASE",
           "tarefa": "TAREFAS SUAS (ERP, Temp, planilha, AnyMarket)",
           "decisao": "SUAS DECISÕES",
           "parado": "PARADO, NÃO DEPENDE DE VOCÊ"}
AVISAVEIS = ("frase", "tarefa", "decisao")  # parado nao chama o usuario
CODIGO = re.compile(r"^P(\d+)$")


def agora() -> str:
    return time.strftime("%Y-%m-%d %H:%M")


def carregar(path: Path) -> dict[str, Any]:
    try:
        dados = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        dados = {}
    if not isinstance(dados, dict):
        dados = {}
    dados.setdefault("proximo", 1)
    dados.setdefault("itens", [])
    return dados


def _salvar(path: Path, dados: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def normalizar(texto: str) -> str:
    """Chave da pergunta do agente: 'Material principal' e 'material  principal' sao a mesma."""
    sem = "".join(c for c in unicodedata.normalize("NFKD", str(texto or "")) if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "-", sem.casefold()).strip("-")


def _novo_codigo(dados: dict[str, Any]) -> str:
    usados = [int(m.group(1)) for i in dados["itens"] if (m := CODIGO.match(str(i.get("codigo", ""))))]
    numero = max([int(dados.get("proximo") or 1), 1 + max(usados, default=0)])
    dados["proximo"] = numero + 1
    return f"P{numero}"


def momento(texto: str) -> datetime | None:
    try:
        return datetime.strptime(str(texto)[:16], "%Y-%m-%d %H:%M")
    except ValueError:
        return None


def sincronizar(path: Path, atuais: list[dict[str, Any]], fechados: set[str] | None = None,
                quando: str | None = None) -> dict[str, Any]:
    """Junta o que as fontes dizem AGORA (lote.json, needs) com o que ja estava na fila.

    `atuais`: itens do script, cada um com chave, tipo, texto (+ lote, sku, etiqueta, sugestao,
    fonte_quando). Item do script que sumiu da fonte = resolvido (feito, ou a resposta entrou).
    Respondido que a fonte ainda traz depois de um preparar MAIS NOVO que a resposta = a resposta nao
    resolveu: volta a aberto, com nota. Pergunta do agente so sai com resposta ou com o lote fechado
    (`fechados`: "LOTE" ou "LOTE|SKU" que fecharam/foram para CONCLUIDO no Trello)."""
    quando = quando or agora()
    fechados = fechados or set()
    dados = carregar(path)
    por_chave = {i["chave"]: i for i in dados["itens"]}
    vistos: set[str] = set()
    for a in atuais:
        chave = a["chave"]
        if chave in vistos:
            continue
        vistos.add(chave)
        it = por_chave.get(chave)
        if it is None:
            it = {"codigo": _novo_codigo(dados), "chave": chave, "origem": "script", "desde": quando,
                  "estado": "aberto"}
            dados["itens"].append(it)
            por_chave[chave] = it
        elif it["estado"] == "resolvido":
            it.update(estado="aberto", desde=quando)
            for k in ("resolvido_em", "resposta", "respondido_em", "avisado_em", "nota"):
                it.pop(k, None)
        elif (it["estado"] == "respondido" and a.get("fonte_quando")
              and str(a["fonte_quando"]) > str(it.get("respondido_em") or "")):
            # 13/10 (<SKU>): "Cinza Chumbo" respondido e a Tray seguia recusando ("Cinza-Chumbo", com hifen)
            it.update(estado="aberto", nota=f"sua resposta \"{it.get('resposta')}\" não resolveu")
            it.pop("avisado_em", None)
        it.update({k: a[k] for k in ("tipo", "lote", "sku", "texto", "etiqueta", "sugestao", "fonte_quando",
                                     "silencioso") if a.get(k) not in (None, "")})
        it["visto"] = quando
    for it in dados["itens"]:
        if it["chave"] in vistos or it["estado"] == "resolvido":
            continue
        if it.get("origem") == "agente":
            lote, sku = str(it.get("lote") or ""), str(it.get("sku") or "")
            if lote in fechados or (sku and f"{lote}|{sku}" in fechados):
                it.update(estado="resolvido", resolvido_em=quando)
            continue
        it.update(estado="resolvido", resolvido_em=quando)
    limite = momento(quando)
    if limite:
        dados["itens"] = [i for i in dados["itens"] if not (
            i["estado"] == "resolvido" and (m := momento(i.get("resolvido_em", ""))) is not None
            and (limite - m).days >= GUARDAR_DIAS)]
    _salvar(path, dados)
    return dados


def perguntar(path: Path, lote: str, sku: str, chave: str, texto: str, sugestao: str = "",
              quando: str | None = None) -> dict[str, Any]:
    """Pergunta que o AGENTE vai fazer (Material sem fonte, modo das fotos, escopo...). Mesma chave no
    mesmo lote/SKU = mesma pergunta: devolve o codigo de antes e, se o usuario ja respondeu ou
    dispensou, a resposta (o agente usa e NAO pergunta de novo)."""
    texto = " ".join(str(texto or "").split())
    if not texto or not normalizar(chave):
        raise ValueError("diga a pergunta (--texto) e o assunto em 1-3 palavras (--chave, ex.: material)")
    quando = quando or agora()
    dados = carregar(path)
    alvo = f"{lote}|{sku}|agente|{normalizar(chave)}"
    it = next((i for i in dados["itens"] if i["chave"] == alvo), None)
    if it is None:
        it = {"codigo": _novo_codigo(dados), "chave": alvo, "origem": "agente", "tipo": "decisao",
              "lote": lote, "sku": sku, "texto": texto, "desde": quando, "estado": "aberto"}
        dados["itens"].append(it)
    elif it["estado"] == "resolvido":
        it.update(estado="aberto", desde=quando, texto=texto)
        for k in ("resolvido_em", "resposta", "respondido_em", "avisado_em"):
            it.pop(k, None)
    elif it["estado"] == "aberto":
        it["texto"] = texto
    if sugestao:
        it["sugestao"] = " ".join(str(sugestao).split())
    _salvar(path, dados)
    return it


def responder(path: Path, codigo: str, resposta: str = "", dispensar: bool = False, reabrir: bool = False,
              quando: str | None = None) -> dict[str, Any]:
    codigo = str(codigo or "").strip().upper()
    dados = carregar(path)
    it = next((i for i in dados["itens"] if i.get("codigo") == codigo), None)
    if it is None:
        raise ValueError(f"{codigo} nao existe na fila (veja: python lote.py fila)")
    if reabrir:
        it["estado"] = "aberto"
        for k in ("resposta", "respondido_em", "avisado_em", "nota", "resolvido_em"):
            it.pop(k, None)
    else:
        resposta = " ".join(str(resposta or "").split())
        if it.get("tipo") == "frase" and not dispensar:
            # aprovar e so pelo aplicar, com a frase dele no --user-quote: a fila nunca guarda aprovacao
            raise ValueError(f"{codigo} e frase de aprovacao: nao se responde aqui -- mostre a tabela e rode o aplicar "
                             "com a frase dele no --user-quote (ou --dispensar, se ele disse que nao precisa)")
        if not resposta and not dispensar:
            raise ValueError("diga a resposta do usuario, com as palavras dele (ou --dispensar)")
        if it["estado"] == "resolvido":
            raise ValueError(f"{codigo} ja foi resolvido em {it.get('resolvido_em')}: nada a responder")
        it.update(estado="dispensado" if dispensar else "respondido", resposta=resposta or "não precisa",
                  respondido_em=quando or agora())
        it.pop("nota", None)
    _salvar(path, dados)
    return it


def para_avisar(path: Path, quando: str | None = None) -> list[dict[str, Any]]:
    """Itens abertos que o usuario ainda nao recebeu como aviso (so o ciclo chama). Marca como avisados:
    o mesmo item nao avisa duas vezes; reaberto (resposta que nao resolveu) avisa de novo."""
    dados = carregar(path)
    novos = [i for i in dados["itens"] if i["estado"] == "aberto" and i.get("tipo") in AVISAVEIS
             and not i.get("avisado_em") and not i.get("silencioso")]
    for i in novos:
        i["avisado_em"] = quando or agora()
    if novos:
        _salvar(path, dados)
    return novos


def _num(codigo: str) -> int:
    m = CODIGO.match(str(codigo))
    return int(m.group(1)) if m else 0


def lote_curto(nome: str, quando: str = "") -> str:
    """auto-20261014-1102 -> '1102' (como o usuario chama o lote); de outro dia, '13/10 1603' -- em 14/10
    havia um 1603 de 13/10 (<SKU>) e outro de 14/10 (<SKU>)."""
    m = re.match(r"^auto-(\d{4})(\d{2})(\d{2})-(\d{4}(?:-\d+)?)$", str(nome or ""))
    if not m:
        return str(nome or "")
    dia = str(quando or agora())[:10]
    return m.group(4) if f"{m.group(1)}-{m.group(2)}-{m.group(3)}" == dia else f"{m.group(3)}/{m.group(2)} {m.group(4)}"


def _dia_hora(texto: str) -> str:
    m = momento(texto)
    return m.strftime("%d/%m %H:%M") if m else str(texto)


def idade(desde: str, quando: str) -> str:
    a, b = momento(desde), momento(quando)
    if not a or not b:
        return ""
    minutos = max(0, int((b - a).total_seconds() // 60))
    if minutos < 60:
        return f"há {minutos} min"
    if minutos < 24 * 60:
        return f"há {minutos // 60}h{minutos % 60:02d}"
    dias = minutos // (24 * 60)
    return f"há {dias} dia" + ("s" if dias > 1 else "")


def curto(texto: str, n: int) -> str:
    """Corta no fim de uma palavra: 'ok faca essas 3 tray dps vamos ver any s' vira '... vamos ver any…'."""
    texto = " ".join(str(texto or "").split())
    if len(texto) <= n:
        return texto
    return texto[:n].rsplit(" ", 1)[0].rstrip(",;:.") + "…"


def _rotulo(i: dict[str, Any], quando: str = "") -> str:
    lote = str(i.get("lote") or "")
    return f"lote {lote_curto(lote, quando)} · " if lote and not lote.startswith(("fotos", "card")) else ""


def linha_do_item(i: dict[str, Any], quando: str) -> str:
    caixa = "[ ] " if i.get("tipo") == "tarefa" else ""
    tempo = (f"pronto {idade(i['desde'], quando)}" if i.get("tipo") == "frase"
             else f"desde {_dia_hora(i['desde'])}")
    extra = f" (sugestão: {i['sugestao']})" if i.get("sugestao") else ""
    nota = f" — {i['nota']}" if i.get("nota") else ""
    return f"  {i['codigo']:<5}{caixa}{_rotulo(i, quando)}{i.get('texto', '')}{extra}{nota} · {tempo}"


# 16/10 (usuario: "meio dificil de entender esses p, p20 p sei la"): o texto tecnico do script ia cru para
# ele e cada SKU virava um Pn com a MESMA frase (P21-P24: "Excel aberto nao encontrado..." 4 vezes).
# Na parte dele: frase simples e itens iguais numa linha so, com os SKUs juntos.
EM_PORTUGUES = [
    (re.compile(r"precificacao: Excel aberto nao encontrado.*", re.I | re.S),
     "abra a planilha de precificação (o agente não achou ela aberta)"),
    (re.compile(r"no ERP \(depois exporte a Temp\):\s*", re.I), "no ERP, depois exporte a Temp: "),
    (re.compile(r"\bsem EAN \(Cod\. Barras\) na Temp", re.I), "falta o EAN"),
    (re.compile(r"\bsem Altura/Largura/Profundidade na Temp(?: \(nem no DOCX\))?", re.I), "faltam as medidas"),
    (re.compile(r"\bsem peso na Temp(?: \(nem no DOCX\))?", re.I), "falta o peso"),
]


def texto_simples(i: dict[str, Any]) -> str:
    """O texto do item sem o SKU (vai a parte) e com as frases tecnicas conhecidas em portugues simples."""
    t = str(i.get("texto") or "")
    sku = str(i.get("sku") or "")
    if sku:
        t = re.sub(rf"\b{re.escape(sku)}\b:?\s*", "", t)
    for padrao, simples in EM_PORTUGUES:
        t = padrao.sub(simples, t)
    t = re.sub(r"\b1\) (?!.*\b2\) )", "", t)  # lista de um item so nao precisa do "1)"
    return " ".join(t.split()).strip(" ·:-")


def _codigos(itens: list[dict[str, Any]]) -> str:
    nums = sorted(_num(i.get("codigo", "")) for i in itens)
    if len(nums) > 2 and nums == list(range(nums[0], nums[-1] + 1)):
        return f"P{nums[0]}–P{nums[-1]}"
    return ", ".join(f"P{n}" for n in nums)


def linhas_juntas(grupo: list[dict[str, Any]], quando: str) -> list[str]:
    """Itens do mesmo lote com o mesmo texto viram UMA linha com os SKUs juntos."""
    juntos: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for i in grupo:
        juntos.setdefault((str(i.get("lote") or ""), texto_simples(i)), []).append(i)
    out = []
    for (_, texto), itens in juntos.items():
        primeiro = itens[0]
        caixa = "[ ] " if primeiro.get("tipo") == "tarefa" else ""
        skus = ", ".join(dict.fromkeys(str(i["sku"]) for i in itens if i.get("sku")))
        tempo = (f"pronto {idade(primeiro['desde'], quando)}" if primeiro.get("tipo") == "frase"
                 else f"desde {_dia_hora(primeiro['desde'])}")
        extra = f" (sugestão: {primeiro['sugestao']})" if primeiro.get("sugestao") else ""
        nota = f" — {primeiro['nota']}" if primeiro.get("nota") else ""
        out.append(f"  {_codigos(itens):<8}{caixa}{_rotulo(primeiro, quando)}{skus + ' — ' if skus else ''}"
                   f"{curto(texto, 160)}{extra}{nota} · {tempo}")
    return out


def linhas_do_usuario(dados: dict[str, Any], quando: str, feitos_hoje: list[str] | None = None) -> list[str]:
    """A parte do AVISO para o usuario: o que depende dele, por bloco, em frase simples e com idade."""
    itens = sorted(dados["itens"], key=lambda i: (str(i.get("desde", "")), _num(i.get("codigo", ""))))
    abertos = [i for i in itens if i["estado"] == "aberto"]
    out = ["PARA VOCÊ — pode responder normal; o número (P3) é só para achar o item, ex.: \"P3 não precisa\"", ""]
    if not [i for i in abertos if i.get("tipo") in AVISAVEIS]:
        out += ["Nada esperando você.", ""]
    for tipo in TIPOS:
        grupo = [i for i in abertos if i.get("tipo") == tipo]
        if grupo:
            out += [TITULOS[tipo], *linhas_juntas(grupo, quando), ""]
    respondidos = [i for i in itens if i["estado"] == "respondido"]
    if respondidos:
        out.append("JÁ RESPONDIDO (o agente usa a sua resposta e não pergunta de novo)")
        out += [f"  {i['codigo']:<5}{_rotulo(i, quando)}{curto(i.get('texto', ''), 90)} → \"{i.get('resposta')}\" "
                f"({_dia_hora(i.get('respondido_em', ''))})" for i in respondidos]
        out.append("")
    dispensados = [i for i in itens if i["estado"] == "dispensado"]
    if dispensados:
        out += [f"Dispensados por você: {', '.join(i['codigo'] for i in dispensados)} "
                f"(para voltar: \"{dispensados[0]['codigo']} volta\")", ""]
    if feitos_hoje:
        out += ["FEITO HOJE", *[f"  {l}" for l in feitos_hoje], ""]
    return out


def linhas_do_agente(dados: dict[str, Any]) -> list[str]:
    """O que o agente precisa saber da fila: respostas ja dadas e itens dispensados (nao perguntar)."""
    out = []
    for i in sorted(dados["itens"], key=lambda i: _num(i.get("codigo", ""))):
        alvo = " ".join(x for x in (f"lote {i['lote']}" if i.get("lote") else "", str(i.get("sku") or "")) if x)
        tag = f" [{i['etiqueta']}]" if i.get("etiqueta") else ""
        if i["estado"] == "respondido":
            out.append(f"[JA RESPONDIDO] {i['codigo']}{tag} {alvo}: resposta do usuario \"{i.get('resposta')}\" "
                       f"({i.get('respondido_em')}) -- pergunta: {str(i.get('texto', ''))[:140]} -- NAO pergunte de "
                       "novo: registre a resposta como o AGENTS.md manda e rode preparar")
        elif i["estado"] == "dispensado":
            out.append(f"[DISPENSADO PELO USUARIO] {i['codigo']}{tag} {alvo}: {str(i.get('texto', ''))[:120]} "
                       f"(\"{i.get('resposta')}\") -- nao peca nem mostre de novo")
    return out
