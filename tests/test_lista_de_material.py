# -*- coding: utf-8 -*-
"""Confere a leitura da lista de material dos PDFs de desenho.

Roda sem pytest:  python tests\\test_lista_de_material.py

Cada caso aqui nasceu de um desenho de verdade. O primeiro e o que fez este
arquivo existir: o IME-MC-1-43469, cujos conjuntos se chamam "COAMING PLATE -
1° EL." e que nao era lido porque o nome termina em ponto.
"""

from __future__ import annotations

import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from app.indicators import parse_material_text  # noqa: E402

resultados: list[tuple[str, bool, str]] = []


def conferir(rotulo: str, condicao: bool, detalhe: str = "") -> None:
    resultados.append((rotulo, bool(condicao), detalhe))


# --- 1) nome de conjunto terminando em ponto (o caso que quebrava) -----------
COAMING = """LISTA DE MATERIAL
1        1 COAMING PLATE - 1° EL.        -        508.31   508.31
1.1      1 CH. 6X150X1374                ASTM A36   9.71     9.71
1.2      1 CH. 6X150X1081                ASTM A36   7.64     7.64
2        1 COAMING PLATE - 2° EL.        -        385.17   385.17
2.1      1 CH. 6X150X1711                ASTM A36  12.09    12.09
3        1 COAMING PLATE - 3° EL.        -        226.03   226.03
PESO TOTAL =   1119.51 Kg
"""
lido = parse_material_text(COAMING, "IME-MC-1-43469_Rev.0.pdf")
conferir("acha os tres conjuntos", lido["assembly_count"] == 3, str(lido["assembly_count"]))
conferir("nao confunde subitem com conjunto",
         all("CH." not in linha["tag"] for linha in lido["assemblies"]),
         str([l["tag"] for l in lido["assemblies"]]))
conferir("mantem o ponto no nome",
         lido["assemblies"][0]["tag"] == "COAMING PLATE - 1° EL.",
         lido["assemblies"][0]["tag"])
conferir("soma dos conjuntos bate com o peso declarado",
         abs(lido["calculated_weight"] - 1119.51) < 0.01, str(lido["calculated_weight"]))
conferir("usa o peso declarado no desenho", lido["declared_weight"] == 1119.51,
         str(lido["declared_weight"]))
conferir("le a revisao do nome do arquivo (com _ antes de Rev)",
         lido["revision"] == "0", lido["revision"])

# O codigo do desenho sai do TEXTO da folha, nao do nome do arquivo.
com_codigo = parse_material_text("IME-MC-1-43469\n" + COAMING, "IME-MC-1-43469_Rev.0.pdf")
conferir("le o codigo do desenho do texto",
         com_codigo["drawing_code"] == "IME-MC-1-43469", com_codigo["drawing_code"])

# --- 2) o formato antigo, com TAG, continua valendo -------------------------
COM_TAG = """LISTA DE MATERIAL
1   2   IME-MC-1-44364-AA-001   -   120,50   241,00
2   1   IME-MC-1-44364-AA-002   -    80,25    80,25
"""
lido = parse_material_text(COM_TAG, "IME-MC-1-44364_Rev.0.pdf")
conferir("formato com TAG: dois conjuntos", lido["assembly_count"] == 2, str(lido["assembly_count"]))
conferir("formato com TAG: quantidade", lido["assemblies"][0]["quantity"] == 2,
         str(lido["assemblies"][0]["quantity"]))
conferir("formato com TAG: peso total do item", lido["assemblies"][0]["weight"] == 241.0,
         str(lido["assemblies"][0]["weight"]))

# --- 3) o formato nomeado que ja existia antes ------------------------------
NOMEADO = """LISTA DE MATERIAL
1   1   PLATAFORMA - 01   -   1621.56   1621.56
2   1   ESCADA - 02       -    340.00    340.00
"""
lido = parse_material_text(NOMEADO, "desenho.pdf")
conferir("formato nomeado: dois conjuntos", lido["assembly_count"] == 2, str(lido["assembly_count"]))
conferir("formato nomeado: nome sem sobra",
         lido["assemblies"][0]["tag"] == "PLATAFORMA - 01", lido["assemblies"][0]["tag"])

# --- 4) lista sem conjunto nenhum nao inventa nada --------------------------
lido = parse_material_text("Um desenho qualquer, sem lista de material.", "x.pdf")
conferir("texto sem lista devolve vazio", lido["assembly_count"] == 0, str(lido["assembly_count"]))

# --- 5) as duas colunas da folha caem na mesma linha ------------------------
# O PyMuPDF le a pagina da esquerda para a direita, entao a linha traz o fim de
# uma coluna e o inicio da outra. Os dois conjuntos precisam ser achados.
DUAS_COLUNAS = (
    "1.16     1 CH. 6X150X2982   ASTM A36   21.07   21.07"
    "            3        1 COAMING PLATE - 3° EL.        -        226.03   226.03\n"
    "1        1 COAMING PLATE - 1° EL.        -        508.31   508.31"
    "            2.42     3 CH. 6X150X174    ASTM A36    1.23    3.69\n"
)
lido = parse_material_text("LISTA DE MATERIAL\n" + DUAS_COLUNAS, "x.pdf")
conferir("duas colunas na mesma linha: acha os dois", lido["assembly_count"] == 2,
         str([l["tag"] for l in lido["assemblies"]]))

# --- 6) o PDF de verdade, quando estiver à mão ------------------------------
PDF_REAL = Path(sys.argv[1]) if len(sys.argv) > 1 else None
if PDF_REAL and PDF_REAL.is_file():
    from app.indicators import parse_material_pdf

    real = parse_material_pdf(PDF_REAL)
    conferir("PDF real: conjuntos encontrados", real["assembly_count"] == 3,
             str(real["assembly_count"]))
    conferir("PDF real: peso total", abs(real["total_weight"] - 1119.51) < 0.01,
             str(real["total_weight"]))

print()
falhas = 0
for rotulo, ok, detalhe in resultados:
    if not ok:
        falhas += 1
    print(f"  {'OK    ' if ok else 'FALHOU'}  {rotulo}" + (f"   [{detalhe}]" if detalhe and not ok else ""))
print(f"\n{len(resultados) - falhas}/{len(resultados)} verificacoes passaram")
sys.exit(1 if falhas else 0)
