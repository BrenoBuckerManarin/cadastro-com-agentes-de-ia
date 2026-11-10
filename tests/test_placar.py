"""placar.py: le sessoes do Codex e acha atritos (07/10). So linhas sinteticas: nao le ~/.codex."""
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "modulos"))

import placar as P  # noqa: E402

BS = chr(92)  # contra-barra: a saida do Codex traz \r\n como TEXTO dentro de string JSON


def linha(ts, tipo, **payload):
    return {"timestamp": f"2026-10-06T{ts}Z", "type": "response_item", "payload": {"type": tipo, **payload}}


def sessao():
    saida_plano = ('{"chunk_id":"a","wall_time_seconds":5.0,"exit_code":2,"session_id":777,"output":"{' + BS + 'r' + BS
                   + 'n  ' + BS + '"ok' + BS + '": false,' + BS + 'r' + BS + 'n  ' + BS + '"problems' + BS + '": ['
                   + BS + 'r' + BS + 'n    {' + BS + 'r' + BS + 'n      ' + BS + '"type' + BS + '": ' + BS
                   + '"characteristics_incomplete' + BS + '"}]}"}')
    return [
        {"timestamp": "2026-10-06T10:00:00Z", "type": "session_meta",
         "payload": {"id": "abc", "thread_source": "user", "cwd": "x"}},
        {"timestamp": "2026-10-06T10:00:00Z", "type": "event_msg", "payload": {"type": "task_started"}},
        linha("10:00:01", "message", role="user", content=[{"type": "input_text", "text": "faz o 20558 ???"}]),
        linha("10:00:02", "message", role="user",
              content=[{"type": "input_text", "text": "Automation: Ciclo do lote ... de novo"}]),
        linha("10:00:03", "custom_tool_call", call_id="c1", name="exec",
              input='const r = await tools.exec_command({cmd:"python scripts/catalog_plan.py --port 19901 --sku 20558",'
                    'yield_time_ms:10000})'),
        linha("10:00:09", "custom_tool_call_output", call_id="c1",
              output=[{"type": "input_text", "text": "Script completed\nWall time 6.0 seconds\nOutput:\n"},
                      {"type": "input_text", "text": saida_plano}]),
        linha("10:00:10", "custom_tool_call", call_id="c2", name="exec",
              input="const r = await tools.write_stdin({session_id:777,yield_time_ms:30000});"),
        linha("10:00:20", "custom_tool_call_output", call_id="c2",
              output=[{"type": "input_text", "text": "Wall time 10.0 seconds\n" + saida_plano}]),
        linha("10:01:00", "message", role="assistant",
              content=[{"type": "output_text", "text": "Não posso otimizar anúncios dessas armas."}]),
        {"timestamp": "2026-10-06T10:02:00Z", "type": "event_msg", "payload": {"type": "task_complete"}},
    ]


class Placar(unittest.TestCase):
    def setUp(self):
        self.s = P.analisar(sessao())
        self.s["nome"] = "Orientar cadastro na Tray"

    def test_assinatura_sku_e_motivo(self):
        c = self.s["chamadas"][0]
        self.assertEqual(c["assinatura"], "catalog_plan.py")
        self.assertEqual(c["skus"], ["20558"])  # 19901 (porta) e 10000 (yield_time_ms) nao sao SKU
        self.assertEqual(c["motivo"], "problems: characteristics_incomplete")
        self.assertEqual(c["wall"], 16.0)  # 6 s do exec + 10 s da leitura do mesmo processo

    def test_write_stdin_herda_o_comando_do_processo(self):
        self.assertEqual(self.s["chamadas"][1]["assinatura"], "catalog_plan.py")

    def test_atritos_agrupam_e_somam(self):
        # 07/10: o write_stdin so le a saida do MESMO processo -- uma falha, nao duas; o tempo soma
        g = P.atritos([self.s])[0]
        self.assertEqual((g["comando"], g["vezes"], g["segundos"]), ("catalog_plan.py", 1, 16.0))
        self.assertEqual(len(P.comandos(self.s)), 1)

    def test_tempo_ativo_e_recusa(self):
        self.assertEqual(self.s["ativo_s"], 120.0)
        self.assertEqual(len(self.s["recusas"]), 1)

    def test_relatorio_filtra_prompt_de_automacao_da_frustracao(self):
        md = P.relatorio([dict(self.s, automacao=False)], 7, datetime(2026, 10, 7, tzinfo=timezone.utc))
        self.assertIn("faz o ##### ???", md)
        self.assertNotIn("Automation: Ciclo", md)
        self.assertIn("RECUSA", md)

    def test_filtro_de_data(self):
        s = P.analisar(sessao(), desde=datetime(2026, 10, 7, tzinfo=timezone.utc))
        self.assertEqual(s["chamadas"], [])

    def test_motivo_de_texto_e_aviso_de_corte(self):
        self.assertEqual(P.motivo_da_falha('{"exit_code":1,"output":"Warning: truncated output (x)"} '
                                           '{"exit_code":1,"output":"sem tabela: rode preparar"}'),
                         "sem tabela: rode preparar")
        self.assertEqual(P.motivo_da_falha('{"exit_code":1,"output":""}'), "exit != 0 (sem mensagem)")


def chunk(stdout, proc=None, exit_code=None, wall=1.0):
    """Saida do exec/write_stdin do Codex: o stdout vai DENTRO de um JSON em texto."""
    obj = {"chunk_id": "x", "wall_time_seconds": wall, "output": stdout}
    if proc:
        obj["session_id"] = proc
    if exit_code is not None:
        obj["exit_code"] = exit_code
    return [{"type": "input_text", "text": f"Script completed\nWall time {wall} seconds\nOutput:\n"},
            {"type": "input_text", "text": json.dumps(obj, ensure_ascii=False)}]


def rodada(n, stdout, user=None, comando="python lote.py preparar auto-20261002-0909"):
    """Uma sessao com um comando do lote que roda em 2 partes (exec + leitura do processo)."""
    proc = 500 + n
    linhas = [{"timestamp": "2026-10-06T10:00:00Z", "type": "session_meta",
               "payload": {"id": f"s{n}", "thread_source": "automation", "cwd": "x"}},
              linha(f"10:0{n}:00", "message", role="user",
                    content=[{"type": "input_text", "text": "Automation: Ciclo do lote"}])]
    if user:
        linhas.append(linha(f"10:0{n}:01", "message", role="user", content=[{"type": "input_text", "text": user}]))
    linhas += [
        linha(f"10:0{n}:02", "custom_tool_call", call_id=f"e{n}", name="exec",
              input=f'const r = await tools.exec_command({{cmd: "{comando}", yield_time_ms: 1000}})'),
        linha(f"10:0{n}:03", "custom_tool_call_output", call_id=f"e{n}",
              output=chunk("preparando 12697...\r\n", proc=proc)),
        linha(f"10:0{n}:04", "custom_tool_call", call_id=f"w{n}", name="exec",
              input=f"const r = await tools.write_stdin({{session_id:{proc}, chars:\"\", yield_time_ms:30000}});"),
        linha(f"10:0{n}:30", "custom_tool_call_output", call_id=f"w{n}", output=chunk(stdout, exit_code=0, wall=24.5)),
    ]
    return P.analisar(linhas) | {"nome": "Ciclo do lote"}


def preparar_json(tray_20575, any_20575="aguardando_anymarket"):
    return "preparando 20575...\r\n" + json.dumps({
        "lote": "auto-20261002-0909", "tudo_pronto": tray_20575 == "pronto",
        "produtos": [{"sku": "20575", "nome": "20575 - TENIS", "tray": tray_20575, "any": any_20575,
                      "any_falta": ["['20601', '20602'] ainda nao apareceram no AnyMarket"]},
                     {"sku": "20500", "nome": "20500 - CHASSI", "tray": "falta",
                      "tray_falta": [{"sku": "20999", "tray": "pronto"}]}]}, indent=2).replace("\n", "\r\n")


class PlacarLote(unittest.TestCase):
    """07/10: o lote diz "tray": "pronto"/"aplicado" e "gravado_verificado"; o placar so lia o tray_run
    e mostrava 4 de 63 SKUs prontos e 2 verificados."""

    def test_leitura_do_processo_nao_e_comando_e_traz_o_json_do_lote(self):
        s = rodada(1, preparar_json("pronto"))
        self.assertEqual([c["assinatura"] for c in P.comandos(s)], ["lote.py preparar"])
        self.assertEqual(P.comandos(s)[0]["lote"]["20575"], {"tray": "pronto", "any": "aguardando_anymarket"})
        self.assertEqual(P.comandos(s)[0]["wall"], 25.5)

    def test_pronto_e_preparos_ate_pronto_depois_separados(self):
        sessoes = [rodada(1, preparar_json("falta", "falta")), rodada(2, preparar_json("pronto")),
                   rodada(3, preparar_json("pronto")), rodada(4, preparar_json("pronto"))]
        r = P.por_sku(sessoes)
        self.assertEqual((r["20575"]["preparos"], r["20575"]["depois"], r["20575"]["pronto"]), (2, 2, True))
        self.assertEqual((r["20500"]["preparos"], r["20500"]["pronto"]), (4, False))
        self.assertNotIn("20999", r)  # objeto aninhado dentro de tray_falta nao e produto do lote
        self.assertNotIn("20601", r)  # SKU citado em texto nao e produto

    def test_aplicado_e_gravado_verificado_contam_como_verificado(self):
        aplicar = json.dumps({"ok": True, "lote": "x", "resultado": [
            {"sku": "20584", "sistema": "tray", "status": "gravado_verificado"},
            {"sku": "20584", "sistema": "any", "status": "falhou"},
            {"sku": "20517", "sistema": "tray", "status": "falhou"}]}, indent=2)
        r = P.por_sku([rodada(1, aplicar, comando="python lote.py aplicar x --user-quote 'aprovo'"),
                       rodada(2, preparar_json("aplicado", "pronto"))])
        self.assertTrue(r["20584"]["verificado"])
        self.assertFalse(r["20517"]["verificado"])
        self.assertTrue(r["20575"]["verificado"] and r["20575"]["pronto"])

    def test_pronto_no_lote_segue_o_resumo_do_lote(self):
        self.assertTrue(P.pronto_no_lote({"tray": "pronto", "any": "aguardando_anymarket"}))
        self.assertTrue(P.pronto_no_lote({"tray": "aplicado", "any": "pronto"}))
        self.assertFalse(P.pronto_no_lote({"tray": "aplicado", "any": "falta"}))
        self.assertFalse(P.pronto_no_lote({"tray": "falta"}))
        self.assertFalse(P.pronto_no_lote({"any": "aguardando_anymarket"}))  # so espera nao e pronto

    def test_saida_cortada_nao_quebra(self):
        s = rodada(1, preparar_json("pronto")[:120])
        self.assertEqual(P.comandos(s)[0]["lote"], {})

    def test_relatorio_separa_ciclo_sozinho_da_rodada_com_o_usuario(self):
        sozinha, com_voce = rodada(1, preparar_json("falta")), rodada(2, preparar_json("pronto"), user="aprovo")
        md = P.relatorio([sozinha, com_voce], 7, datetime(2026, 10, 7, tzinfo=timezone.utc))
        self.assertIn("Automacao sozinha (ciclo): 1 rodadas", md)
        self.assertIn("Automacao com o usuario no chat: 1 rodadas", md)
        self.assertIn("| 20575 | 2 | 0 | sim |", md)


if __name__ == "__main__":
    unittest.main()
