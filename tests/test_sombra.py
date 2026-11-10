"""Modo sombra (07/10): mede aprovacao de primeira por classe; nunca bloqueia."""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "modulos"))

import sombra  # noqa: E402


def item(sku="20614", tarefa=None, tray_tabela="Titulo: Luneta Veyron 3-12x44", any_extra=None):
    return {"sku": sku, "tarefa": tarefa, "any": {"skus": [sku], **(any_extra or {})},
            "tray": {"status": "pronto", "tabela": tray_tabela},
            "any_prep": {"status": "pronto", "tabela": "| 1 | create_slot | ML |", "titulos_exatos": ["Luneta"]}}


class Sombra(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "lotes" / sombra.ARQUIVO

    def tearDown(self):
        self.tmp.cleanup()

    def test_classes(self):
        self.assertEqual(sombra.classe(item(), "tray"), "tray_produto_novo")
        self.assertEqual(sombra.classe(item(tarefa="adicionar_variacoes"), "tray"), "tray_cor_nova")
        self.assertEqual(sombra.classe(item(tarefa="kit"), "tray"), "tray_kit")
        self.assertEqual(sombra.classe(item(any_extra={"somente_vinculo_tray": True}), "any"), "any_vinculo_tray")
        self.assertEqual(sombra.classe(item(any_extra={"so_canais": ["MERCADO_LIVRE"]}), "any"), "any_canal_faltando")
        self.assertEqual(sombra.classe(item(tarefa="adicionar_variacoes"), "any"), "any_variacao_nova")
        self.assertEqual(sombra.classe(item(), "any"), "any_produto_novo")

    def test_aprovado_de_primeira_e_tabela_repetida_nao_conta_como_versao(self):
        lote = {"nome": "auto-x", "items": [item()]}
        sombra.marcar_tabela(lote, {"20614": ["tray", "any"]})
        sombra.marcar_tabela(lote, {"20614": ["tray", "any"]})  # preparar de novo sem mudar nada
        linhas = sombra.registrar_aplicar(lote, {"20614": {"tray": {"status": "gravado_verificado"},
                                                           "any": {"status": "gravado_verificado"}}},
                                          "aprovo", "2026-10-07 13:00", self.log)
        self.assertEqual([(l["sistema"], l["versoes"], l["de_primeira"]) for l in linhas],
                         [("tray", 1, True), ("any", 1, True)])
        self.assertEqual(lote["items"][0]["sombra"], {})  # gravado: a proxima fase conta do zero

    def test_correcao_do_usuario_vira_versao_nova(self):
        # 02/10: "vc colocou nos titulos ar 15 mas nao e o nome do produto" -> texto reescrito
        lote = {"nome": "auto-x", "items": [item(tray_tabela="Titulo: Carabina AR15 Dione")]}
        sombra.marcar_tabela(lote, {"20614": ["tray"]})
        lote["items"][0]["tray"]["tabela"] = "Titulo: Carabina Rossi Dione"
        sombra.marcar_tabela(lote, {"20614": ["tray"]})
        linhas = sombra.registrar_aplicar(lote, {"20614": {"tray": {"status": "gravado_verificado"}}},
                                          "aprovo", "2026-10-07 13:00", self.log)
        self.assertEqual((linhas[0]["versoes"], linhas[0]["de_primeira"]), (2, False))

    def test_falha_mantem_a_contagem_para_a_nova_tentativa(self):
        lote = {"nome": "auto-x", "items": [item()]}
        sombra.marcar_tabela(lote, {"20614": ["tray"]})
        sombra.registrar_aplicar(lote, {"20614": {"tray": {"status": "falhou"}}}, "aprovo", "q", self.log)
        self.assertIn("tray", lote["items"][0]["sombra"])
        sombra.registrar_aplicar(lote, {"20614": {"tray": {"status": "gravado_verificado"}}}, "tenta", "q", self.log)
        r = sombra.resumo(sombra.ler(self.log))
        self.assertEqual(r, [{"classe": "tray_produto_novo", "gravados": 1, "de_primeira": 1, "falhas": 1,
                              "pct_de_primeira": 100}])

    def test_sistema_fora_da_tabela_nao_e_registrado(self):
        lote = {"nome": "auto-x", "items": [item()]}
        sombra.marcar_tabela(lote, {"20614": ["tray"]})  # Any aguardando: nao entrou na tabela
        linhas = sombra.registrar_aplicar(lote, {"20614": {"any": {"status": "falhou"}}}, "q", "q", self.log)
        self.assertEqual(linhas, [])
        self.assertIn("Sem aplicacoes", sombra.tabela_md([]))


class SoContaCorrecaoDoUsuario(unittest.TestCase):
    """08/10 (20607): 4 versoes eram o Codex tirando aviso de SEO antes de mostrar; o usuario viu 1."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "lotes" / sombra.ARQUIVO

    def tearDown(self):
        self.tmp.cleanup()

    def _tres_versoes(self):
        lote = {"nome": "auto-x", "items": [item()]}
        for minuto, meta in ((13, "Luneta"), (14, "Zoom de 1-6x"), (19, "1-6x24")):
            lote["items"][0]["tray"]["tabela"] = f"Meta: {meta}"
            sombra.marcar_tabela(lote, {"20614": ["tray"]}, agora=datetime(2026, 10, 8, 11, minuto, tzinfo=timezone.utc))
        return lote

    def test_codex_se_corrigindo_antes_de_mostrar_e_de_primeira(self):
        lote = self._tres_versoes()
        aprovou = [datetime(2026, 10, 8, 11, 8, tzinfo=timezone.utc), datetime(2026, 10, 8, 11, 20, tzinfo=timezone.utc)]
        linha = sombra.registrar_aplicar(lote, {"20614": {"tray": {"status": "gravado_verificado"}}},
                                         "aprovo", "q", self.log, mensagens=aprovou)[0]
        self.assertEqual((linha["versoes"], linha["de_primeira"], linha["versoes_geradas"]), (1, True, 3))

    def test_mensagem_do_usuario_entre_versoes_e_correcao(self):
        lote = self._tres_versoes()
        pediu = [datetime(2026, 10, 8, 11, 16, tzinfo=timezone.utc)]  # entre a 2a e a 3a
        linha = sombra.registrar_aplicar(lote, {"20614": {"tray": {"status": "gravado_verificado"}}},
                                         "aprovo", "q", self.log, mensagens=pediu)[0]
        self.assertEqual((linha["versoes"], linha["de_primeira"]), (2, False))

    def test_sem_saber_das_mensagens_conta_todas(self):
        lote = self._tres_versoes()
        linha = sombra.registrar_aplicar(lote, {"20614": {"tray": {"status": "gravado_verificado"}}},
                                         "aprovo", "q", self.log, mensagens=None)[0]
        self.assertEqual((linha["versoes"], linha["de_primeira"]), (3, False))

    def test_padrao_em_teste_nao_le_sessao_real_e_conta_todas(self):
        lote = self._tres_versoes()
        linha = sombra.registrar_aplicar(lote, {"20614": {"tray": {"status": "gravado_verificado"}}},
                                         "aprovo", "q", self.log)[0]
        self.assertEqual(linha["versoes"], 3)

    def test_estado_de_antes_sem_momentos_conta_como_antes(self):
        lote = {"nome": "auto-x", "items": [item()]}
        lote["items"][0]["sombra"] = {"tray": {"classe": "tray_produto_novo", "versoes": ["a"]}}
        sombra.marcar_tabela(lote, {"20614": ["tray"]})
        s = lote["items"][0]["sombra"]["tray"]
        self.assertNotIn("momentos", s)
        self.assertEqual(sombra.versoes_vistas(s, []), 2)

    def test_le_so_as_falas_do_usuario_nas_sessoes(self):
        pasta = Path(self.tmp.name) / "sessions"
        dia = pasta / "2026" / "10" / "08"
        dia.mkdir(parents=True)

        def fala(hora, texto, role="user"):
            return json.dumps({"timestamp": f"2026-10-08T{hora}Z", "type": "response_item",
                               "payload": {"type": "message", "role": role,
                                           "content": [{"type": "input_text", "text": texto}]}})
        (dia / "rollout-x.jsonl").write_text("\n".join([
            fala("10:57:11.000", "Automation: Ciclo do lote ..."),
            fala("10:57:12.000", "<environment_context>...</environment_context>"),
            fala("11:08:44.000", "pode seguir acabei de enviar o produto ao any"),
            fala("11:09:00.000", "vou rodar o ciclo", role="assistant"),
            fala("11:10:00.000", "<send_user_message_question_reply> [categoria: Lunetas]"),
            fala("11:20:37.000", "aprovo"),
        ]), encoding="utf-8")
        falas = sombra.mensagens_do_usuario(datetime(2026, 10, 8, 11, 0, tzinfo=timezone.utc),
                                            datetime(2026, 10, 8, 11, 19, tzinfo=timezone.utc), pasta)
        self.assertEqual(falas, [datetime(2026, 10, 8, 11, 8, 44, tzinfo=timezone.utc),
                                 datetime(2026, 10, 8, 11, 10, tzinfo=timezone.utc)])

    def test_teste_nunca_le_as_sessoes_reais(self):
        agora = datetime.now(timezone.utc)
        self.assertIsNone(sombra.mensagens_do_usuario(agora, agora))


class PlacarComSombra(unittest.TestCase):
    def test_placar_mostra_a_secao_do_modo_sombra(self):
        from datetime import datetime, timezone
        import placar
        linhas = [{"classe": "any_vinculo_tray", "resultado": "gravado_verificado", "de_primeira": True}] * 3 + \
                 [{"classe": "tray_produto_novo", "resultado": "gravado_verificado", "de_primeira": False}]
        md = placar.relatorio([], 7, datetime(2026, 10, 7, tzinfo=timezone.utc), linhas)
        self.assertIn("## 6. Modo sombra", md)
        self.assertIn("| any_vinculo_tray | 3 | 3 | 100% | 0 |", md)
        self.assertIn("| tray_produto_novo | 1 | 0 | 0% | 0 |", md)

    def test_placar_soma_o_lote_e_o_catalog_apply_direto_sem_as_previas(self):
        import json
        import placar
        with tempfile.TemporaryDirectory() as tmp:
            lote_log, any_log = Path(tmp) / "l.jsonl", Path(tmp) / "a.jsonl"
            lote_log.write_text(json.dumps({"classe": "tray_cor_nova", "resultado": "gravado_verificado",
                                            "de_primeira": True}) + "\n", encoding="utf-8")
            any_log.write_text("\n".join(json.dumps(l) for l in (
                {"evento": "previa", "classe": "any_vinculo_tray"},
                {"evento": "aplicacao", "classe": "any_vinculo_tray", "resultado": "gravado_verificado",
                 "de_primeira": True})) + "\n", encoding="utf-8")
            tray_log = Path(tmp) / "t.jsonl"
            tray_log.write_text(json.dumps({"evento": "aplicacao", "classe": "tray_kit",
                                            "resultado": "falhou", "de_primeira": False}) + "\n", encoding="utf-8")
            r = placar.registros_sombra(lote_log, any_log, tray_log)
        self.assertEqual(sorted(l["classe"] for l in r), ["any_vinculo_tray", "tray_cor_nova", "tray_kit"])


if __name__ == "__main__":
    unittest.main()
