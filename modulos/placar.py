"""Placar da operacao: le as sessoes do Codex e do Claude Code e diz ONDE o tempo foi e O QUE travou.

Uso:
    python placar.py [--dias 7] [--saida diagnosticos]

Gera diagnosticos/placar-AAAA-MM-DD.md com:
  1. sessoes (conversa x automacao sozinha x automacao com o usuario): tempo ativo, comandos, falhas;
  2. ATRITOS ordenados por custo (tempo perdido em comandos que falharam + repeticoes);
  3. comandos repetidos na mesma sessao (laco de tentativa);
  4. por SKU: quantos preparos ate ficar pronto, se foi verificado na Tray;
  5. recusas do modelo e frases de frustracao do usuario.

Por que existe (07/10): o loop de melhoria dependia do usuario trazer a sessao ("olha a
sessao"). Com o placar, o analista (Claude ou GPT) pega o TOPO da lista de atritos, conserta a
causa e mede de novo na semana seguinte. So LE arquivos locais; nao escreve em Tray/Any/Trello/ERP.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sombra

# 14/10: desde 13/10 o ciclo roda no Claude Code (tarefa agendada + agente ciclo-do-lote); o placar so
# lia o Codex e mostrou "1 conversa" num dia de 3 lotes. Agora le os dois.
CLAUDE = Path.home() / ".claude" / "projects" / "C--Users-User--claude"
CICLO_CLAUDE = '<scheduled-task name="ciclo-do-lote"'
CODEX = Path.home() / ".codex"
SESSOES = CODEX / "sessions"
INDICE = CODEX / "session_index.jsonl"
AQUI = Path(__file__).resolve().parent

SCRIPT = re.compile(r"([\w-]+\.py)[\"']?(?:\s+(?!-)([a-z][\w-]*))?")  # 14/10: "C:/.../lote.py" preparar
SKU = re.compile(r"(?<!\d)(1\d{4}|2\d{4})(?!\d)")
WALL = re.compile(r"Wall time ([\d.]+) seconds")
EXIT = re.compile(r'"exit_code\\?"\s*:\s*(\d+)')
STATUS = re.compile(r'"status\\?"\s*:\s*\\?"([a-z_]{2,40})\\?"')
STATUS_BOM = {"ok", "ready_for_approval", "applied_verified", "fulfilled", "completed", "pronto", "aplicado"}
PROXIMO = re.compile(r'"next\\?"\s*:\s*\\?"([^"\\]{3,110})')
SAIDA_TEXTO = re.compile(r'"output\\?"\s*:\s*\\?"([^"\\]{3,110})')
SESSAO_PROC = re.compile(r'"session_id\\?"\s*:\s*(\d+)')
# JSON dentro de string: as quebras chegam como os caracteres \r\n, nao como espaco
W = r'(?:\s|\\[rnt])*'
PRIMEIRO_MOTIVO = re.compile(r'"(?:needs|problems|falhas_de_validacao|falta|any_falta|error)\\?"\s*:' + W
                             + r'\[?' + W + r'\\?"([^"\\]{3,110})')
# problems como objetos (catalog_plan/catalog_apply): [{"type": "pricing_unavailable", "detail": ...}]
PROBLEMA_TIPO = re.compile(r'"(?:problems|blockers|bloqueios)\\?"\s*:' + W + r'\[' + W + r'\{' + W
                           + r'\\?"type\\?"\s*:\s*\\?"([\w-]{3,60})')
ERRO_PY = re.compile(r"(\w+(?:Error|Exception): [^\r\n\\\"]{0,100})")
ERRO_PS = re.compile(r"(Cannot [^\r\n\\\"]{0,90}|is not recognized[^\r\n\\\"]{0,60}|"
                     r"n[aã]o (?:pode|foi poss[ií]vel) [^\r\n\\\"]{0,80})")
# recusa = o MODELO se nega pelo tipo de produto. "Ainda nao posso preparar: a categoria nao tem
# mapa" (05/10, suporte de alvo) e bloqueio de regra, nao recusa -- o placar contava errado.
RECUSA = re.compile(r"n[aã]o (?:posso |consigo )?(?:ajudar|preparar|otimizar|criar|escrever|promover)[^.]{0,80}"
                    r"(?:venda|vender|promov|armas?\b|destinad)|minha limita[cç][aã]o|I can(?:no|')t help", re.I)
FRUSTRACAO = re.compile(r"\?\?\?|de novo|mesmo erro|como n[aã]o|travou|demor|errado|n[aã]o era", re.I)
NAO_SKU = re.compile(r"(?:yield_time_ms|max_output_tokens|timeout_ms|timeout|session_id|turnLimit|cell_id|port)"
                     r"\\?\"?\s*[:=]\s*\d+|--port\s+\d+|\b(?:<porta>|<SKU>)\b")
# mensagem do usuario que e prompt colado/automacao, nao frustracao
NAO_E_CONVERSA = ("Automation:", "# Files mentioned", "Você vai ser o analista", "O Claude corrigiu")
SEGREDO = re.compile(r"[A-Za-z0-9]{32,}")


def _ts(texto: str) -> datetime | None:
    try:
        return datetime.fromisoformat(texto.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _texto(conteudo) -> str:
    if isinstance(conteudo, str):
        return conteudo
    if isinstance(conteudo, list):
        return "\n".join(_texto(c.get("text", "") if isinstance(c, dict) else c) for c in conteudo)
    return json.dumps(conteudo, ensure_ascii=False) if conteudo is not None else ""


def _limpo(texto: str, n: int = 110) -> str:
    texto = SEGREDO.sub("***", " ".join(str(texto).split()))
    return re.sub(r"\d", "#", texto)[:n] if n else texto


# 07/10: o placar so reconhecia ready_for_approval/applied_verified (do tray_run). O lote.py diz
# "tray": "pronto"/"aplicado" por produto e "gravado_verificado" no relatorio -- 63 SKUs preparados
# apareciam como 4 prontos e 2 verificados. Agora le o JSON que o lote imprime.
ESTADO_PRONTO = {"pronto", "aplicado", "ja_feito"}
ESTADO_ESPERA = {"aguardando_anymarket", "aguardando_tray"}


def _stdout(saida: str) -> str:
    """O stdout do processo: o campo "output" de cada chunk do exec/write_stdin do Codex."""
    dec, partes = json.JSONDecoder(), []
    for m in re.finditer(r'\{"chunk_id"', saida):
        try:
            obj, _ = dec.raw_decode(saida, m.start())
        except ValueError:
            continue
        partes.append(str(obj.get("output") or ""))
    return "".join(partes) if partes else saida


def estados_do_lote(stdout: str) -> dict[str, dict]:
    """sku -> {"tray": estado, "any": estado, "tray_gravado": bool} pelo JSON do lote.py
    (`produtos` do preparar/tabela, `resultado` do aplicar/relatorio). Saida cortada: {}."""
    dec, out = json.JSONDecoder(), {}
    for m in re.finditer(r"(?:^|\n)(\{)", stdout):  # o JSON do lote abre na coluna 0 (indent=2)
        try:
            obj, _ = dec.raw_decode(stdout, m.start(1))
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        for p in obj.get("produtos") or []:
            if isinstance(p, dict) and p.get("sku"):
                r = out.setdefault(str(p["sku"]), {})
                r.update({k: p[k] for k in ("tray", "any") if isinstance(p.get(k), str)})
        for p in obj.get("resultado") or []:
            if isinstance(p, dict) and p.get("sku"):
                r = out.setdefault(str(p["sku"]), {})
                if p.get("status") == "gravado_verificado":
                    r[f"{p.get('sistema')}_gravado"] = True
    return out


def pronto_no_lote(r: dict) -> bool:
    """Mesma regra do resumo() do lote: tudo pronto/esperando e pelo menos um sistema pronto."""
    estados = [r[k] for k in ("tray", "any") if r.get(k)]
    return (bool(estados) and all(e in ESTADO_PRONTO | ESTADO_ESPERA for e in estados)
            and any(e in ESTADO_PRONTO for e in estados))


def assinatura(entrada: str, ferramenta: str = "") -> str:
    """'tray_run.py prepare', 'lote.py conferir'... ou o 1o comando de shell."""
    m = SCRIPT.search(entrada)
    if m:
        return f"{m.group(1)} {m.group(2) or ''}".strip()
    cmd = re.search(r"cmd\s*[:=]\s*(?:String\.raw)?\s*[`'\"](.{0,60})", entrada, re.S)
    primeira = (cmd.group(1) if cmd else entrada).strip().split()
    if not cmd and entrada.lstrip().startswith("{"):
        return f"ferramenta {ferramenta or 'js'}"  # polling de celula JS, sem comando proprio
    return f"shell {primeira[0][:30]}" if primeira else "shell"


def motivo_da_falha(saida: str) -> str:
    """O motivo mais util que a saida oferece, do mais especifico ao mais generico."""
    m = PROBLEMA_TIPO.search(saida)
    if m:
        return f"problems: {m.group(1)}"
    for padrao in (PRIMEIRO_MOTIVO, ERRO_PY):
        m = padrao.search(saida)
        if m:
            return _limpo(m.group(1))
    ruins = [s for s in STATUS.findall(saida) if s not in STATUS_BOM]
    if ruins:
        return ruins[0]
    for padrao in (PROXIMO, ERRO_PS):
        m = padrao.search(saida)
        if m:
            return _limpo(m.group(1))
    for m in SAIDA_TEXTO.finditer(saida):  # o aviso de corte do Codex nao e motivo
        if not m.group(1).startswith("Warning: truncated"):
            return _limpo(m.group(1))
    return "exit != 0 (sem mensagem)"


def analisar(linhas: list[dict], desde: datetime | None = None) -> dict:
    """Uma sessao -> chamadas, mensagens, turnos. Funcao PURA (linhas ja lidas do .jsonl)."""
    meta = next((l.get("payload") or {} for l in linhas if l.get("type") == "session_meta"), {})
    chamadas: dict[str, dict] = {}
    ordem: list[str] = []
    usuario, recusas, inicio_turno, ativo = [], [], None, 0.0
    # session_id do exec -> call_id do comando. O write_stdin so LE a saida do mesmo processo: o que
    # ele traz (exit, status, tempo, JSON) vai para o comando original, e ele mesmo nao conta como
    # comando novo (07/10: "lote.py aplicar x87" eram 87 leituras de UM aplicar).
    processos: dict[str, str] = {}
    primeiro = ultimo = None
    for l in linhas:
        ts = _ts(l.get("timestamp", ""))
        if desde and ts and ts < desde:
            continue
        if ts:
            primeiro, ultimo = primeiro or ts, ts
        p = l.get("payload") or {}
        tipo = p.get("type")
        if tipo == "task_started":
            inicio_turno = ts
        elif tipo == "task_complete" and inicio_turno and ts:
            ativo += (ts - inicio_turno).total_seconds()
            inicio_turno = None
        elif tipo == "message":
            texto = _texto(p.get("content"))
            if p.get("role") == "user" and not texto.lstrip().startswith(("<", "# AGENTS.md")):
                usuario.append((ts, texto))
            elif p.get("role") == "assistant" and RECUSA.search(texto):
                recusas.append((ts, _limpo(RECUSA.search(texto).group(0) + " | " + texto, 160)))
        elif tipo in ("function_call", "custom_tool_call"):
            entrada = _texto(p.get("arguments") or p.get("input") or "")
            cid = p.get("call_id") or str(len(ordem))
            sig = assinatura(entrada, p.get("name") or "")
            pai = None
            if "write_stdin" in entrada:
                m = re.search(r"session_id\s*:\s*(\d+)", entrada)
                pai = processos.get(m.group(1)) if m else None
                sig = chamadas[pai]["assinatura"] if pai in chamadas else sig
            chamadas[cid] = {"ts": ts, "nome": p.get("name"), "assinatura": sig,
                             "skus": sorted(set(SKU.findall(NAO_SKU.sub(" ", entrada)))), "wall": 0.0, "exit": None,
                             "status": [], "motivo": "", "leitura": pai in chamadas, "pai": pai, "stdout": ""}
            ordem.append(cid)
        elif tipo in ("function_call_output", "custom_tool_call_output"):
            c = chamadas.get(p.get("call_id"))
            if not c:
                continue
            dono = c["pai"] if c["leitura"] else p.get("call_id")
            c = chamadas[dono]
            saida = _texto(p.get("output"))
            if c["assinatura"].startswith("lote.py"):
                c["stdout"] += _stdout(saida)
            for proc in SESSAO_PROC.findall(saida):
                processos.setdefault(proc, dono)
            c["wall"] += sum(float(x) for x in WALL.findall(saida))
            codigos = [int(x) for x in EXIT.findall(saida)]
            if codigos:
                c["exit"] = max(codigos)
            c["status"] += STATUS.findall(saida)
            if (c["exit"] or 0) != 0 or set(c["status"]) & {"needs_input", "blocked", "write_failed", "lote_em_uso"}:
                c["motivo"] = c["motivo"] or motivo_da_falha(saida)
    if inicio_turno and ultimo:
        ativo += (ultimo - inicio_turno).total_seconds()
    for c in chamadas.values():
        c["lote"] = estados_do_lote(c.pop("stdout")) if c["stdout"] else {}
    return {"id": meta.get("id", ""), "automacao": meta.get("thread_source") == "automation",
            "cwd": meta.get("cwd", ""), "inicio": primeiro, "fim": ultimo, "ativo_s": ativo,
            "chamadas": [chamadas[c] for c in ordem], "usuario": usuario, "recusas": recusas}


EXIT_CLAUDE = re.compile(r"Exit code (\d+)")


def _texto_claude(conteudo) -> str:
    if isinstance(conteudo, str):
        return conteudo
    if isinstance(conteudo, list):
        return "\n".join(_texto_claude(c.get("text", "") if isinstance(c, dict) else c) for c in conteudo)
    return "" if conteudo is None else str(conteudo)


def analisar_claude(linhas: list[dict], desde: datetime | None = None, sub: list[dict] | None = None) -> dict:
    """Uma sessao do Claude Code (+ as linhas dos subagentes dela) no MESMO formato de `analisar`.
    Funcao PURA. Comando = Bash/PowerShell; o tempo e do pedido ate o resultado."""
    abertos: dict[str, dict] = {}
    chamadas, usuario, recusas, eventos = [], [], [], []
    sessao, cwd, automacao = "", "", False
    primeiro = ultimo = None
    for l in sorted(linhas + (sub or []), key=lambda x: x.get("timestamp") or ""):
        ts = _ts(l.get("timestamp", ""))
        if not ts or (desde and ts < desde):
            continue
        primeiro, ultimo = primeiro or ts, ts
        sessao, cwd = sessao or l.get("sessionId", ""), cwd or l.get("cwd", "")
        lateral = bool(l.get("isSidechain"))
        msg = l.get("message") or {}
        conteudo = msg.get("content")
        if l.get("type") == "assistant":
            for b in conteudo if isinstance(conteudo, list) else []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use" and b.get("name") in ("Bash", "PowerShell"):
                    entrada = str((b.get("input") or {}).get("command") or "")
                    abertos[b.get("id")] = {
                        "ts": ts, "nome": b.get("name"), "assinatura": assinatura(entrada, b.get("name") or ""),
                        "skus": sorted(set(SKU.findall(NAO_SKU.sub(" ", entrada)))), "wall": 0.0, "exit": None,
                        "status": [], "motivo": "", "leitura": False, "pai": None, "lote": {}}
                elif b.get("type") == "text" and RECUSA.search(b.get("text") or ""):
                    texto = b.get("text") or ""
                    recusas.append((ts, _limpo(RECUSA.search(texto).group(0) + " | " + texto, 160)))
            if not lateral:
                eventos.append(("A", ts))
        elif l.get("type") == "user":
            if isinstance(conteudo, list) and any(isinstance(b, dict) and b.get("type") == "tool_result"
                                                  for b in conteudo):
                for b in conteudo:
                    if not (isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") in abertos):
                        continue
                    c = abertos.pop(b["tool_use_id"])
                    saida = _texto_claude(b.get("content"))
                    c["wall"] = (ts - c["ts"]).total_seconds()
                    m = EXIT_CLAUDE.search(saida[:200])
                    c["exit"] = int(m.group(1)) if m else (1 if b.get("is_error") else 0)
                    c["status"] = STATUS.findall(saida)
                    if (c["exit"] or 0) != 0 or set(c["status"]) & {"needs_input", "blocked", "write_failed", "lote_em_uso"}:
                        c["motivo"] = motivo_da_falha(saida)
                    if c["assinatura"].startswith("lote.py"):
                        c["lote"] = estados_do_lote(saida)
                    chamadas.append(c)
                continue
            texto = _texto_claude(conteudo)
            if texto.lstrip().startswith(CICLO_CLAUDE):
                automacao = True
            if not lateral and texto.strip() and not texto.lstrip().startswith("<"):
                usuario.append((ts, texto))
                eventos.append(("U", ts))
    # tempo ativo: rodada automatica = do comeco ao fim; conversa = cada pedido ate a ultima resposta
    ativo = 0.0
    if eventos:
        inicio = eventos[0][1]
        for i, (tipo, t) in enumerate(eventos):
            if tipo == "U" and i:
                ativo += (eventos[i - 1][1] - inicio).total_seconds()
                inicio = t
        ativo += (eventos[-1][1] - inicio).total_seconds()
    return {"id": sessao, "automacao": automacao, "cwd": cwd, "inicio": primeiro, "fim": ultimo, "ativo_s": ativo,
            "chamadas": sorted(chamadas, key=lambda c: c["ts"]), "usuario": usuario, "recusas": recusas}


def _ler_jsonl(arq: Path) -> list[dict]:
    linhas = []
    with open(arq, encoding="utf-8", errors="replace") as fh:
        for l in fh:
            try:
                linhas.append(json.loads(l))
            except ValueError:
                continue
    return linhas


def carregar_claude(desde: datetime, base: Path | None = None) -> list[dict]:
    """Sessoes do Claude Code que sao do lote: a tarefa agendada do ciclo (e a conversa do usuario nela)
    ou sessao aberta numa pasta do projeto. Nunca a do analista."""
    base = base or CLAUDE
    sessoes = []
    for arq in sorted(base.glob("*.jsonl")) if base.exists() else []:
        if datetime.fromtimestamp(arq.stat().st_mtime, timezone.utc) < desde:
            continue
        inicio = []
        with open(arq, encoding="utf-8", errors="replace") as fh:  # so o comeco decide (a do analista tem 30+ MB)
            for _, l in zip(range(20), fh):
                try:
                    inicio.append(json.loads(l))
                except ValueError:
                    continue
        do_lote = any(CICLO_CLAUDE in _texto_claude((l.get("message") or {}).get("content"))[:200]
                      for l in inicio if l.get("type") == "user") or any(
            "Codex Auto" in str(l.get("cwd") or "") for l in inicio[:5])
        if not do_lote:
            continue
        linhas = _ler_jsonl(arq)
        sub = [l for f in sorted((base / arq.stem / "subagents").glob("*.jsonl")) for l in _ler_jsonl(f)] \
            if (base / arq.stem / "subagents").exists() else []
        s = analisar_claude(linhas, desde, sub)
        if not s["chamadas"] and not s["usuario"]:
            continue
        s["nome"] = "Ciclo do lote (Claude)" if s["automacao"] else f"Claude {s['id'][:8]}"
        sessoes.append(s)
    return sessoes


def falhou(c: dict) -> bool:
    return bool(c["motivo"])


def comandos(s: dict) -> list[dict]:
    """Os comandos de verdade da sessao, sem as leituras de saida (write_stdin) de um processo."""
    return [c for c in s["chamadas"] if not c.get("leitura")]


def conversou(s: dict) -> bool:
    """Rodada do ciclo em que o usuario falou no chat (aprovar, decidir, 'continua o lote')."""
    return any(not t.lstrip().startswith(NAO_E_CONVERSA) for _, t in s["usuario"])


def atritos(sessoes: list[dict]) -> list[dict]:
    """Falhas agrupadas por (comando, motivo), ordenadas por tempo perdido."""
    grupos: dict[tuple, dict] = {}
    for s in sessoes:
        for c in s["chamadas"]:
            if not falhou(c):
                continue
            g = grupos.setdefault((c["assinatura"], c["motivo"]), {
                "comando": c["assinatura"], "motivo": c["motivo"], "vezes": 0, "segundos": 0.0,
                "sessoes": set(), "skus": set()})
            g["vezes"] += 1
            g["segundos"] += c["wall"]
            g["sessoes"].add(s["nome"])
            g["skus"].update(c["skus"])
    # custo: tempo de maquina + 60 s por ocorrencia (o agente le, pensa e tenta de novo)
    return sorted(grupos.values(), key=lambda g: -(g["segundos"] + 60 * g["vezes"]))


def repeticoes(sessoes: list[dict], minimo: int = 3) -> list[dict]:
    """Mesmo script+subcomando+SKU rodado varias vezes na mesma sessao (laco de tentativa)."""
    saida = []
    for s in sessoes:
        cont = Counter((c["assinatura"], ",".join(c["skus"])) for c in s["chamadas"]
                       if c["skus"] and ".py" in c["assinatura"])
        for (cmd, skus), n in cont.items():
            if n >= minimo:
                saida.append({"sessao": s["nome"], "comando": cmd, "skus": skus, "vezes": n})
    return sorted(saida, key=lambda r: -r["vezes"])


def por_sku(sessoes: list[dict]) -> dict[str, dict]:
    """Preparos ate o 1o pronto (tray_run: ready_for_approval; lote: estado do produto) e depois dele,
    e se a Tray foi gravada e verificada (applied_verified / "aplicado" / gravado_verificado).
    Os preparos DEPOIS de pronto nao sao laco: em geral e o ciclo reconferindo o AnyMarket (07/10)."""
    out: dict[str, dict] = defaultdict(lambda: {"preparos": 0, "depois": 0, "bloqueios": Counter(), "pronto": False,
                                                "verificado": False, "primeiro": None, "verificado_em": None})
    for s in sessoes:
        for c in comandos(s):
            if ".py" not in c["assinatura"]:
                continue
            lote = c.get("lote") or {}
            skus = set(c["skus"] if len(c["skus"]) <= 12 else []) | set(lote)
            for sku in skus:
                r = out[sku]
                r["primeiro"] = r["primeiro"] or c["ts"]
                if c["assinatura"].endswith(("prepare", "preparar")):
                    r["depois" if r["pronto"] else "preparos"] += 1
                if c["motivo"] and sku in c["skus"]:
                    r["bloqueios"][c["motivo"]] += 1
                e = lote.get(sku) or {}
                if ("ready_for_approval" in c["status"] and sku in c["skus"]) or pronto_no_lote(e):
                    r["pronto"] = True
                tray_ok = ("applied_verified" in c["status"] and sku in c["skus"]) or e.get("tray") == "aplicado" \
                    or e.get("tray_gravado")
                if tray_ok and not r["verificado"]:
                    r["verificado"], r["verificado_em"], r["pronto"] = True, c["ts"], True
    return dict(out)


def _min(segundos: float) -> str:
    return f"{segundos / 60:.0f} min" if segundos >= 90 else f"{segundos:.0f} s"


def relatorio(sessoes: list[dict], dias: int, hoje: datetime, sombra_linhas: list[dict] | None = None) -> str:
    conversas = [s for s in sessoes if not s["automacao"]]
    autos = [s for s in sessoes if s["automacao"]]
    linhas = [f"# Placar da operacao — {hoje:%d/%m/%Y} (ultimos {dias} dias)", "",
              f"{len(conversas)} conversa(s), {len(autos)} rodada(s) automatica(s). "
              "Custo de um atrito = tempo de maquina + 1 min por ocorrencia.", "",
              "## 1. Sessoes (conversas)", "",
              "| Sessao | Tempo ativo | Msgs do usuario | Comandos | Falharam |", "|---|---:|---:|---:|---:|"]
    for s in sorted(conversas, key=lambda s: -s["ativo_s"]):
        linhas.append(f"| {s['nome']} | {_min(s['ativo_s'])} | {len(s['usuario'])} | {len(comandos(s))} | "
                      f"{sum(falhou(c) for c in s['chamadas'])} |")
    # 07/10: 37 de 44 rodadas eram vazias (~30 s); os 353 min vinham das 7 em que o usuario operou
    # no chat do ciclo. Somadas, pareciam "a automacao custa 6 h".
    for rotulo, grupo in (("Automacao sozinha (ciclo)", [s for s in autos if not conversou(s)]),
                          ("Automacao com o usuario no chat", [s for s in autos if conversou(s)])):
        if grupo:
            cmds = sum(len(comandos(s)) for s in grupo)
            falhas = sum(falhou(c) for s in grupo for c in s["chamadas"])
            linhas += ["", f"{rotulo}: {len(grupo)} rodadas, {_min(sum(s['ativo_s'] for s in grupo))} ativas, "
                       f"{cmds} comandos, {falhas} falharam."]
    linhas += ["", "## 2. Atritos (do mais caro para o mais barato)", "",
               "| # | Comando | Motivo | Vezes | Tempo | Onde | SKUs |", "|---:|---|---|---:|---:|---|---|"]
    for i, g in enumerate(atritos(sessoes)[:25], 1):
        skus = ", ".join(sorted(g["skus"])[:6]) + ("…" if len(g["skus"]) > 6 else "")
        linhas.append(f"| {i} | `{g['comando']}` | {g['motivo']} | {g['vezes']} | {_min(g['segundos'])} | "
                      f"{', '.join(sorted(g['sessoes']))[:60]} | {skus} |")
    rep = repeticoes(sessoes)
    if rep:
        linhas += ["", "## 3. Comandos repetidos na mesma sessao", "",
                   "| Sessao | Comando | SKUs | Vezes |", "|---|---|---|---:|"]
        linhas += [f"| {r['sessao']} | `{r['comando']}` | {r['skus']} | {r['vezes']} |" for r in rep[:15]]
    skus = por_sku(sessoes)
    if skus:
        # o fluxo normal e 2 preparos ate pronto (o 1o pede os textos); 5+ sem ficar pronto e laco
        prontos = sorted(r["preparos"] for r in skus.values() if r["pronto"] and r["preparos"])
        mediana = prontos[len(prontos) // 2] if prontos else 0
        lacos = sum(1 for r in skus.values() if r["preparos"] >= 5)
        linhas += ["", "## 4. Por SKU", "",
                   f"SKUs com preparo: {sum(1 for r in skus.values() if r['preparos'] or r['depois'])}. "
                   f"Chegaram a pronto: {len(prontos)} (mediana de {mediana} preparos ate pronto; o normal e 2). "
                   f"Com 5+ preparos ate pronto: {lacos}. "
                   f"Verificados na Tray: {sum(r['verificado'] for r in skus.values())}.", "",
                   "\"Depois\" = preparos com o produto ja pronto (ciclo reconferindo o AnyMarket, espera do ERP "
                   "ou de decisao): nao e laco.", "",
                   "| SKU | Preparos ate pronto | Depois | Pronto | Verificado | Principal bloqueio |",
                   "|---|---:|---:|:-:|:-:|---|"]
        for sku, r in sorted(skus.items(), key=lambda kv: (-kv[1]["preparos"], -kv[1]["depois"], kv[0]))[:40]:
            if not (r["preparos"] or r["depois"] or r["verificado"] or r["bloqueios"]):
                continue
            motivo = r["bloqueios"].most_common(1)[0][0] if r["bloqueios"] else ""
            linhas.append(f"| {sku} | {r['preparos']} | {r['depois']} | {'sim' if r['pronto'] else '—'} | "
                          f"{'sim' if r['verificado'] else '—'} | {motivo} |")
    recusas = [(s["nome"], ts, t) for s in sessoes for ts, t in s["recusas"]]
    frust = [(s["nome"], ts, _limpo(t, 140)) for s in conversas for ts, t in s["usuario"]
             if FRUSTRACAO.search(t) and len(t) < 600 and not t.lstrip().startswith(NAO_E_CONVERSA)]
    if recusas or frust:
        linhas += ["", "## 5. Recusas do modelo e frustracao do usuario", ""]
        linhas += [f"- RECUSA [{n} {ts:%d/%m %H:%M}] {t}" for n, ts, t in recusas[:10] if ts]
        linhas += [f"- USUARIO [{n} {ts:%d/%m %H:%M}] {t}" for n, ts, t in frust[:15] if ts]
    if sombra_linhas is not None:
        # 07/10: evidencia para tirar o usuario da aprovacao, por classe (ver sombra.py)
        linhas += ["", "## 6. Modo sombra: tabelas aprovadas sem mudar nada (desde 07/10, acumulado)", "",
                   sombra.tabela_md(sombra_linhas).rstrip()]
    linhas += ["", "## Como usar", "",
               "Pegue o atrito #1, ache a causa no codigo (nao no prompt), conserte com teste e rode o",
               "placar de novo na semana seguinte: o atrito tem que sumir ou cair. Ver HANDOFF-ANALISTA.md."]
    return "\n".join(linhas) + "\n"


def nomes() -> dict[str, str]:
    out = {}
    if INDICE.exists():
        for l in INDICE.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                d = json.loads(l)
                out[d["id"]] = d.get("thread_name") or out.get(d["id"], "")
            except (ValueError, KeyError):
                continue
    return out


def carregar(dias: int, agora: datetime) -> list[dict]:
    desde = agora - timedelta(days=dias)
    titulo = nomes()
    sessoes = []
    for arq in sorted(SESSOES.rglob("*.jsonl")):
        if datetime.fromtimestamp(arq.stat().st_mtime, timezone.utc) < desde:
            continue
        linhas = []
        with open(arq, encoding="utf-8", errors="replace") as fh:
            for l in fh:
                try:
                    linhas.append(json.loads(l))
                except ValueError:
                    continue
        s = analisar(linhas, desde)
        if not s["chamadas"] and not s["usuario"]:
            continue
        s["nome"] = titulo.get(s["id"]) or s["id"][:8]
        sessoes.append(s)
    return sessoes + carregar_claude(desde)


ANY_SOMBRA = AQUI.parent / "Any operação" / "runtime" / "sombra.jsonl"
TRAY_SOMBRA = AQUI.parent / "cadastrar-produtos-tray" / "runtime" / "sombra.jsonl"


def registros_sombra(lote_log: Path | None = None, any_log: Path | None = None,
                     tray_log: Path | None = None) -> list[dict]:
    """Aplicacoes do lote + do catalog_apply e do tray_run diretos (chats do AnyMarket e da Tray)."""
    do_lote = sombra.ler(lote_log or AQUI / "lotes" / sombra.ARQUIVO)
    diretos = [l for log in (any_log or ANY_SOMBRA, tray_log or TRAY_SOMBRA)
               for l in sombra.ler(log) if l.get("evento") == "aplicacao"]
    return do_lote + diretos


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dias", type=int, default=7)
    ap.add_argument("--saida", default=str(AQUI / "diagnosticos"))
    args = ap.parse_args(argv)
    agora = datetime.now(timezone.utc)
    sessoes = carregar(args.dias, agora)
    md = relatorio(sessoes, args.dias, agora.astimezone(), registros_sombra())
    destino = Path(args.saida) / f"placar-{agora.astimezone():%Y-%m-%d}.md"
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(md, encoding="utf-8")
    print(f"placar: {destino}")
    for i, g in enumerate(atritos(sessoes)[:5], 1):
        print(f"  {i}. {g['comando']} — {g['motivo']} ({g['vezes']}x, {_min(g['segundos'])})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
