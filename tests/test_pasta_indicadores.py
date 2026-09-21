# -*- coding: utf-8 -*-
"""Confere onde a janela de arquivos abre ao importar o PDF dos Indicadores.

Roda sem pytest:  python tests\test_pasta_indicadores.py

A regra que este arquivo protege: a janela abre na subpasta do desenho, dentro
da pasta geral configurada. O nome da subpasta costuma trazer um complemento
depois do codigo ("IME-MC-1-43470 - Cutouts de grade"), entao o prefixo tambem
vale. Sem pasta configurada, nada muda em relacao ao comportamento antigo.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from app.service import ControleService  # noqa: E402

resultados: list[tuple[str, bool, str]] = []


def conferir(rotulo: str, condicao: bool, detalhe: str = "") -> None:
    resultados.append((rotulo, bool(condicao), detalhe))


class BancoDeMentira:
    """So o necessario: o servico le a pasta geral por aqui."""

    def __init__(self, raiz: str) -> None:
        self.valor = raiz

    def setting(self, chave: str, padrao: str = "") -> str:
        return self.valor if chave == "indicators_root" else padrao


def servico_com(raiz: str) -> ControleService:
    servico = ControleService.__new__(ControleService)   # sem abrir banco nenhum
    servico.db = BancoDeMentira(raiz)
    return servico


with tempfile.TemporaryDirectory() as temporaria:
    base = Path(temporaria) / "GATO DO MATO"
    (base / "IME-MC-1-43469").mkdir(parents=True)
    (base / "IME-MC-1-43470 - Cutouts de grade").mkdir()
    (base / "IME-MC-1-44364").mkdir()

    servico = servico_com(str(base))

    conferir("nome exato: abre na subpasta do desenho",
             servico.indicators_start_dir("IME-MC-1-43469") == str(base / "IME-MC-1-43469"),
             servico.indicators_start_dir("IME-MC-1-43469"))

    conferir("nome com complemento depois: tambem vale",
             servico.indicators_start_dir("IME-MC-1-43470")
             == str(base / "IME-MC-1-43470 - Cutouts de grade"),
             servico.indicators_start_dir("IME-MC-1-43470"))

    conferir("desenho sem subpasta: abre na pasta geral",
             servico.indicators_start_dir("IME-MC-1-99999") == str(base),
             servico.indicators_start_dir("IME-MC-1-99999"))

    conferir("sem codigo do projeto: abre na pasta geral",
             servico.indicators_start_dir("") == str(base),
             servico.indicators_start_dir(""))

    conferir("pasta geral que nao existe: nao interfere",
             servico_com(str(base / "nao existe")).indicators_start_dir("IME-MC-1-43469") == "",
             servico_com(str(base / "nao existe")).indicators_start_dir("IME-MC-1-43469"))

conferir("sem pasta configurada: comportamento antigo",
         servico_com("").indicators_start_dir("IME-MC-1-43469") == "",
         servico_com("").indicators_start_dir("IME-MC-1-43469"))

print()
falhas = 0
for rotulo, ok, detalhe in resultados:
    if not ok:
        falhas += 1
    print(f"  {'OK    ' if ok else 'FALHOU'}  {rotulo}" + (f"   [{detalhe}]" if detalhe and not ok else ""))
print(f"\n{len(resultados) - falhas}/{len(resultados)} verificacoes passaram")
sys.exit(1 if falhas else 0)
