# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from app.nuvem import sincronizacao  # noqa: E402


class SincronizacaoNaAberturaTests(unittest.TestCase):
    def test_desligada_nao_faz_transferencia(self):
        with patch.object(sincronizacao, "ler_config", return_value={"ligada": False}), \
                patch.object(sincronizacao, "enviar") as enviar, \
                patch.object(sincronizacao, "receber") as receber:
            resultado = sincronizacao.sincronizar_na_abertura(object())

        self.assertEqual(resultado, {"executada": False, "motivo": "desligada"})
        enviar.assert_not_called()
        receber.assert_not_called()

    def test_home_recebe_sem_forcar(self):
        banco = object()
        with patch.object(sincronizacao, "ler_config", return_value={"ligada": True, "ambiente": "home"}), \
                patch.object(sincronizacao, "receber", return_value={"novidade": False}) as receber:
            resultado = sincronizacao.sincronizar_na_abertura(banco)

        receber.assert_called_once_with(banco, False)
        self.assertEqual(resultado["acao"], "receber")
        self.assertEqual(resultado["ambiente"], "HOME")

    def test_externo_envia(self):
        with patch.object(sincronizacao, "ler_config", return_value={"ligada": True, "ambiente": "EXTERNO"}), \
                patch.object(sincronizacao, "enviar", return_value={"gerado_em": "agora"}) as enviar:
            resultado = sincronizacao.sincronizar_na_abertura(object())

        enviar.assert_called_once_with()
        self.assertEqual(resultado["acao"], "enviar")
        self.assertEqual(resultado["ambiente"], "EXTERNO")

    def test_ambiente_ausente_nao_transfere(self):
        with patch.object(sincronizacao, "ler_config", return_value={"ligada": True}), \
                patch.object(sincronizacao, "enviar") as enviar, \
                patch.object(sincronizacao, "receber") as receber:
            resultado = sincronizacao.sincronizar_na_abertura(object())

        self.assertEqual(resultado, {"executada": False, "motivo": "ambiente_nao_definido"})
        enviar.assert_not_called()
        receber.assert_not_called()


if __name__ == "__main__":
    unittest.main()
