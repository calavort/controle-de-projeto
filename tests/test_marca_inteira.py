# -*- coding: utf-8 -*-
"""Confere o reconhecimento de item pelo balao inteiro (escadas 9, 10, 11).

Roda sem pytest e sem Tekla:  python tests\\test_marca_inteira.py

O caso que fez este arquivo existir: o IME-MC-1-44361 (montagem de escadas). Os
itens dali nao tem posicao decimal no modelo (nao existe 9.1), so o balao com o
numero do item. O painel so lia decimais e mostrava 1 de 17 com tudo detalhado.
A regra nova vale apenas para item SEM decimais; item com decimais (16 -> 16.1)
continua so pela regra decimal, que e a regra historica do programa.

Os objetos do Tekla aqui sao de mentira: so tem o que o leitor de marcas usa.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
ARQUIVO = RAIZ / "app" / "progresso_detalhamento" / "Progresso de detalhamento.py"
spec = importlib.util.spec_from_file_location("progresso_detalhamento_prog", ARQUIVO)
prog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prog)

resultados: list[tuple[str, bool, str]] = []


def conferir(rotulo: str, condicao: bool, detalhe: str = "") -> None:
    resultados.append((rotulo, bool(condicao), detalhe))


# --- objetos de mentira ------------------------------------------------------
class Enum:
    def __init__(self, itens):
        self._itens = list(itens)
        self._i = -1

    def MoveNext(self):
        self._i += 1
        return self._i < len(self._itens)

    @property
    def Current(self):
        return self._itens[self._i]

    def GetEnumerator(self):
        return Enum(self._itens)


class Conteudo:
    def __init__(self, texto):
        self._texto = texto

    def GetUnformattedString(self):
        return self._texto


class Atributos:
    def __init__(self, texto):
        self.Content = Conteudo(texto)


class Mark:
    """Balao de marca. ``preso`` diz se aponta para uma peca do desenho."""

    def __init__(self, texto, preso=True):
        self.Attributes = Atributos(texto)
        self.IsAssociativeNote = False
        self._preso = preso

    def Select(self):
        return True

    def GetObjects(self):
        return None

    def GetRelatedObjects(self):
        return Enum(["peca"] if self._preso else [])


class Vista:
    def __init__(self, nome, objetos, tipo="FrontView", pagina=None):
        self.Name = nome
        self._objetos = objetos
        self.ViewType = type("T", (), {"ToString": lambda s: tipo})()
        self.pagina = pagina

    def GetAllObjects(self):
        return Enum(self._objetos)


class Folha:
    def __init__(self, vistas):
        self._vistas = vistas

    def GetViews(self):
        return Enum(self._vistas)


class Desenho:
    def __init__(self, vistas):
        self._folha = Folha(vistas)

    def GetSheet(self):
        return self._folha


def ponte():
    """TeklaBridge sem Tekla: paginas e lista de excecao viram atalhos simples."""
    b = prog.TeklaBridge(lambda _msg: None)
    b.detect_temporary_pages_cached = lambda sheet, identity, refresh=False: ([], True)
    b.page_for_view = lambda view, pages: view.pagina
    b.is_component_base_view_ignored = lambda view, idx, names: (False, view.Name)
    return b


def varrer(vistas, esperados, inteiros, modo="conjunto"):
    b = ponte()
    mapa = {n: f"TAG-{n}" for n in esperados}
    marked, item_views, item_pages, decimais, sub_pages, sub_views, stats = b.scan_drawing(
        Desenho(vistas), mapa, None, [], modo, inteiros
    )
    return marked, item_pages, decimais, stats


# --- 1) o caso das escadas ---------------------------------------------------
ESPERADOS = list(range(1, 17))                   # 1..16 (16 tem a peca 16.1)
SO_INTEIRO = set(range(1, 16))                    # 1..15 nao tem decimais

vista_az = Vista("AZ", [Mark("9"), Mark("10"), Mark("11")], pagina=6)
marked, paginas, decimais, stats = varrer([vista_az], ESPERADOS, SO_INTEIRO)
conferir("balao 9, 10 e 11 da vista AZ marcam os tres itens", marked == {9, 10, 11}, str(sorted(marked)))
conferir("a folha do balao fica registrada", paginas.get(9) == {6}, str(paginas))
conferir("nenhuma posicao decimal inventada", not decimais, str(decimais))
conferir("estatistica diz quais itens vieram do balao",
         stats["integer_evidence_items"] == [9, 10, 11], str(stats["integer_evidence_items"]))

# --- 2) a regra de antes continua: sem a lista de itens inteiros, balao nao vale ---
marked, _p, _d, _s = varrer([vista_az], ESPERADOS, None)
conferir("sem itens 'so inteiro' o balao continua ignorado (regra historica)", marked == set(), str(sorted(marked)))

# --- 3) item com decimais so conta pelo decimal, nunca pelo balao ------------
marcas = [Mark("16"), Mark("3")]
marked, _p, _d, _s = varrer([Vista("V1", marcas)], ESPERADOS, SO_INTEIRO)
conferir("balao 16 (item com decimais) nao conta", 16 not in marked, str(sorted(marked)))
conferir("balao 3 (item so inteiro) conta", 3 in marked, str(sorted(marked)))
marked, _p, decimais, _s = varrer([Vista("V2", [Mark("16.1")])], ESPERADOS, SO_INTEIRO)
conferir("marca 16.1 conta o item 16 pela regra decimal", 16 in marked and (16, 1) in decimais,
         f"{sorted(marked)} {sorted(decimais)}")

# --- 4) visao geral: 3D da folha 1 com balao de quase todos os itens ---------
tres_d = Vista("3D", [Mark(str(n)) for n in range(1, 16)], tipo="ModelView", pagina=1)
marked, _p, _d, stats = varrer([tres_d, vista_az], ESPERADOS, SO_INTEIRO)
conferir("vista com balao de 15 itens e visao geral e nao conta", marked == {9, 10, 11}, str(sorted(marked)))
conferir("a visao geral e contada nas estatisticas", stats["integer_overview_views"] == 1,
         str(stats["integer_overview_views"]))

# --- 5) projeto pequeno: nunca confunde com visao geral ----------------------
tudo = Vista("Planta", [Mark("1"), Mark("2"), Mark("3")])
marked, _p, _d, stats = varrer([tudo], [1, 2, 3], {1, 2, 3})
conferir("projeto de 3 itens: vista com os 3 baloes conta", marked == {1, 2, 3} and stats["integer_overview_views"] == 0,
         f"{sorted(marked)} {stats['integer_overview_views']}")

# --- 6) rotulo de eixo (numero solto, sem peca) nao e marca de item ----------
eixo = Vista("Eixos", [Mark("4", preso=False), Mark("2", preso=True)])
marked, _p, _d, stats = varrer([eixo], ESPERADOS, SO_INTEIRO)
conferir("'4' do eixo (sem peca) nao conta", 4 not in marked, str(sorted(marked)))
conferir("'2' preso a uma peca conta", 2 in marked, str(sorted(marked)))
conferir("o rotulo solto e contado nas estatisticas", stats["integer_unlinked_marks"] == 1,
         str(stats["integer_unlinked_marks"]))

# --- 7) DetailView continua fora no modo por conjunto ------------------------
detalhe = Vista("Detalhe 1", [Mark("5")], tipo="DetailView")
marked, _p, _d, _s = varrer([detalhe], ESPERADOS, SO_INTEIRO)
conferir("balao dentro de DetailView nao conta (regra historica)", marked == set(), str(sorted(marked)))

# --- 8) modo por componente nao usa o balao ----------------------------------
marked, _p, _d, _s = varrer([vista_az], ESPERADOS, SO_INTEIRO, modo="subitem")
conferir("por componente: balao inteiro nao entra", marked == set(), str(sorted(marked)))

# --- 9) texto da marca: so a linha INTEIRA e numero ---------------------------
conferir("'9' e item", prog.TeklaBridge.integer_items_from_text("9") == {9})
conferir("'9.1' nao e balao inteiro", prog.TeklaBridge.integer_items_from_text("9.1") == set())
conferir("'Ø9' nao e balao", prog.TeklaBridge.integer_items_from_text("Ø9") == set())
conferir("linha '9' junto de outro texto", prog.TeklaBridge.integer_items_from_text("STEL\n9") == {9})

# --- 10) modelo: quem tem decimais e quem so tem a posicao inteira ------------
class Peca:
    def __init__(self, posicao):
        self.posicao = posicao


class Conjunto:
    def __init__(self, numero, posicoes):
        self.numero = numero
        self.pecas = [Peca(p) for p in posicoes]


b = ponte()
b.assembly_parts = lambda conj: conj.pecas
b.subitem_from_model_object = lambda peca: prog.parse_subitem_position(peca.posicao)
conferir("conjunto com 16.1 tem decimais", b.assembly_decimal_parents(Conjunto(16, ["16", "16.1"])) == {16})
conferir("escada so com a posicao inteira nao tem decimais",
         b.assembly_decimal_parents(Conjunto(9, ["9", "9", "9"])) == set())

modelo = [Conjunto(n, [str(n)] * 4) for n in range(1, 16)] + [Conjunto(16, ["16", "16.1"])]
b.model = type("M", (), {"GetModelObjectSelector": lambda s: type("S", (), {
    "GetAllObjectsWithType": lambda s2, t: Enum(modelo)})()})()
b.ModelObject = type("MO", (), {"ModelObjectEnum": type("E", (), {"ASSEMBLY": 1})})
b.item_number_for_assembly = lambda conj: conj.numero
b.support_tag_for_assembly = lambda conj, p, f: f"TAG-{conj.numero}"
itens, estatisticas = b.expected_items("FRMW", "AwevaFRMW")
conferir("expected_items acha os 16 itens", sorted(itens) == list(range(1, 17)), str(sorted(itens)))
conferir("itens so com a posicao inteira: 1 a 15", estatisticas["integer_items"] == list(range(1, 16)),
         str(estatisticas["integer_items"]))
conferir("item com decimais: so o 16", estatisticas["decimal_parents"] == [16], str(estatisticas["decimal_parents"]))

# --- 11) historico: o que o balao ja acha pronto e historico, sem tempo -------
def resultado(detalhados, integer_evidence=()):
    detalhados = sorted(detalhados)
    return {
        "project_key": "p44361", "project_number": "IME-MC-1-44361", "model_path": "m", "model_name": "m",
        "drawing_name": "d", "ok": True, "progress_mode": "conjunto",
        "total": 17, "detailed": len(detalhados), "missing": 17 - len(detalhados),
        "tracking_detailed_items": [{"number": n, "tag": f"TAG-{n}", "pages": [5]} for n in detalhados],
        "tracking_detailed_numbers": detalhados, "detailed_numbers": detalhados,
        "tracking_missing_items": [], "item_views": {str(n): [f"V{n}"] for n in detalhados},
        "item_pages": {str(n): [5] for n in detalhados}, "ignored_detail_items": [],
        "integer_evidence_numbers": list(integer_evidence),
    }


with tempfile.TemporaryDirectory() as pasta:
    historico = prog.DetailHistory(Path(pasta) / "estatisticas.json")
    historico.update(resultado([2]), 15)                      # como estava: so o item 2
    projeto = historico.data["projects"]["p44361"]
    conferir("antes: so o item 2 conhecido", projeto["known_detailed"] == [2], str(projeto["known_detailed"]))

    quinze = list(range(1, 16))
    historico.update(resultado(quinze, integer_evidence=[n for n in quinze if n != 2]), 15)
    projeto = historico.data["projects"]["p44361"]
    conferir("com a regra nova: os 15 ficam conhecidos", projeto["known_detailed"] == quinze, str(projeto["known_detailed"]))
    conferir("nao abre sessao 'em detalhamento' para os 14 de uma vez", not projeto.get("active_session"),
             str(projeto.get("active_session")))
    conferir("os 14 entram como historico, sem tempo",
             all(projeto["items"][str(n)]["historical"] and not projeto["items"][str(n)]["tracked_seconds"]
                 for n in quinze if n != 2), "")

    historico.update(resultado(quinze + [16], integer_evidence=[n for n in quinze if n != 2]), 15)
    projeto = historico.data["projects"]["p44361"]
    sessao = projeto.get("active_session") or {}
    conferir("item novo depois da linha de base entra em detalhamento", sessao.get("items") == [16], str(sessao))

# --- 12) analyze() de ponta a ponta: o painel passa a mostrar os itens --------
b = ponte()
vistas = [
    Vista("3D", [Mark(str(n)) for n in range(1, 16)], tipo="ModelView", pagina=1),
    Vista("AZ", [Mark("9"), Mark("10"), Mark("11")], pagina=6),
    Vista("L1", [Mark("1"), Mark("2"), Mark("3")], pagina=3),
    Vista("Conj", [Mark("16.1")], pagina=14),
]
b.require_active_drawing = lambda raiz: Desenho(vistas)
b.model_identity = lambda d: {"project_key": "p44361", "project_number": "IME-MC-1-44361", "model_path": "m",
                              "model_name": "m", "drawing_name": "d", "drawing_signature": ""}
b.expected_items_cached = lambda ident, p, f: (
    {n: f"TAG-{n}" for n in range(1, 17)},
    {"integer_items": list(range(1, 16)), "decimal_parents": [16], "cached": True},
)
resp = b.analyze({"modo_progresso": "conjunto", "vistas_base_ignoradas": []})
conferir("analyze: detalhados = 1,2,3,9,10,11 e 16",
         resp["detailed_numbers"] == [1, 2, 3, 9, 10, 11, 16], str(resp["detailed_numbers"]))
conferir("analyze: total 16 e faltando 9", (resp["total"], resp["missing"]) == (16, 9), f"{resp['total']} {resp['missing']}")
conferir("analyze: itens vindos do balao ficam registrados",
         resp["integer_evidence_numbers"] == [1, 2, 3, 9, 10, 11], str(resp["integer_evidence_numbers"]))
conferir("analyze: a folha de cada item aparece",
         {i["number"]: i["pages"] for i in resp["detailed_items"]}.get(9) == [6], str(resp["detailed_items"]))

resp = b.analyze({"modo_progresso": "conjunto", "vistas_base_ignoradas": []})
conferir("analyze repetido da o mesmo resultado", resp["detailed_numbers"] == [1, 2, 3, 9, 10, 11, 16], str(resp["detailed_numbers"]))

print()
falhas = 0
for rotulo, ok, detalhe in resultados:
    if not ok:
        falhas += 1
    print(f"  {'OK    ' if ok else 'FALHOU'}  {rotulo}" + (f"   [{detalhe}]" if detalhe and not ok else ""))
print(f"\n{len(resultados) - falhas}/{len(resultados)} verificacoes passaram")
sys.exit(1 if falhas else 0)
