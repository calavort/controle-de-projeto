from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any


DRAWING_CODE_RE = re.compile(r"\bIME-MC-\d+-\d+\b", re.IGNORECASE)
# O \b antes de "Rev" exigia que o caractere anterior NAO fosse de palavra — e o
# sublinhado é. Com isso "IME-MC-1-43469_Rev.0.pdf", que é como os desenhos
# chegam aqui, ficava sem revisão. A condição certa é outra: "Rev" não pode ser
# o fim de uma palavra maior (PREVISAO), e quem garante isso é a letra.
REVISION_FILE_RE = re.compile(r"(?<![A-Za-z])Rev\.?\s*([A-Z0-9]+)\b", re.IGNORECASE)
TOTAL_WEIGHT_RE = re.compile(r"PESO\s+TOTAL\s*=\s*([\d.,]+)\s*Kg", re.IGNORECASE)
ASSEMBLY_RE = re.compile(
    r"(?<![\d.])(\d{1,4})\s+(\d+)\s+"
    r"([A-Z0-9]+(?:-[A-Z0-9]+){4,})\s+-\s+"
    r"([\d.,]+)\s+([\d.,]+)",
    re.IGNORECASE,
)

# Alguns desenhos estruturais usam nomes de conjunto em vez de TAGs, por
# exemplo: "1  1  PLATAFORMA - 01  -  1621.56  1621.56". O material
# desses itens principais aparece como um traço. O padrão abaixo é usado
# somente quando a tabela tradicional com TAGs não foi encontrada.
#
# A descrição aceita qualquer coisa que não atravesse a linha. A versão
# anterior listava os caracteres permitidos e exigia que o nome TERMINASSE em
# letra ou número — e por causa disso um desenho inteiro deixava de ser lido só
# porque o conjunto se chamava "COAMING PLATE - 1° EL.", com o ponto no fim.
# Quem delimita a linha não é a descrição: é o material do conjunto, que vem
# sempre como um traço isolado, seguido dos dois pesos. É neles que o padrão
# se ancora.
NAMED_ASSEMBLY_RE = re.compile(
    r"(?<![\d.])(\d{1,4})\s+(\d+)\s+"      # item e quantidade
    r"([A-ZÀ-ÖØ-Þ0-9][^\n]*?)"                # descrição do conjunto
    r"\s+-\s+"                                # material: um traço isolado
    r"([\d.,]+)\s+([\d.,]+)",                # peso unitário e peso total
    re.IGNORECASE,
)


def parse_number(value: Any) -> float:
    text = str(value or "").strip().replace(" ", "")
    if not text:
        return 0.0
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return 0.0


def parse_material_text(text: str, source_name: str = "") -> dict[str, Any]:
    raw_text = str(text or "")
    assemblies_by_item: dict[int, dict[str, Any]] = {}
    for match in ASSEMBLY_RE.finditer(raw_text):
        item = int(match.group(1))
        quantity = int(match.group(2))
        tag = match.group(3).upper().strip()
        unit_weight = parse_number(match.group(4))
        total_weight = parse_number(match.group(5))
        assemblies_by_item[item] = {
            "item": str(item),
            "quantity": quantity,
            "tag": tag,
            "weight": round(total_weight, 3),
            "unit_weight": round(unit_weight, 3),
            "source_type": "pdf",
        }

    # Compatibilidade com listas em que os conjuntos principais são nomeados
    # (PLATAFORMA - 01, ESCADA - 02 etc.) e não possuem uma TAG extensa.
    if not assemblies_by_item:
        for match in NAMED_ASSEMBLY_RE.finditer(raw_text):
            item = int(match.group(1))
            quantity = int(match.group(2))
            name = re.sub(r"\s+", " ", match.group(3)).strip(" -").upper()
            unit_weight = parse_number(match.group(4))
            total_weight = parse_number(match.group(5))
            assemblies_by_item[item] = {
                "item": str(item),
                "quantity": quantity,
                "tag": name,
                "weight": round(total_weight, 3),
                "unit_weight": round(unit_weight, 3),
                "source_type": "pdf",
            }

    total_match = TOTAL_WEIGHT_RE.search(raw_text)
    declared_weight = parse_number(total_match.group(1)) if total_match else 0.0
    assemblies = [assemblies_by_item[key] for key in sorted(assemblies_by_item)]
    calculated_weight = round(sum(float(row["weight"]) for row in assemblies), 3)

    drawing_match = DRAWING_CODE_RE.search(raw_text)
    drawing_code = drawing_match.group(0).upper() if drawing_match else ""
    revision_match = REVISION_FILE_RE.search(Path(source_name).name)
    revision = revision_match.group(1).upper() if revision_match else ""

    return {
        "drawing_code": drawing_code,
        "revision": revision,
        "declared_weight": round(declared_weight, 3),
        "total_weight": round(declared_weight or calculated_weight, 3),
        "calculated_weight": calculated_weight,
        "assemblies": assemblies,
        "assembly_count": len(assemblies),
        "page_count": 0,
    }



def _material_pages_text(pages: list[str]) -> str:
    material_pages = [
        page for page in pages
        if "LISTA DE MATERIAL" in str(page or "").upper()
    ]
    return "\n".join(material_pages or pages)

def _extract_with_pypdf(source: Path) -> tuple[list[str], int, str]:
    try:
        from pypdf import PdfReader
    except ImportError:
        return [], 0, "pypdf não instalado"

    logging.getLogger("pypdf").setLevel(logging.ERROR)
    try:
        reader = PdfReader(str(source))
        pages: list[str] = []
        for page in reader.pages:
            try:
                page_text = page.extract_text(extraction_mode="layout") or ""
            except TypeError:  # compatibilidade com versões antigas do pypdf
                page_text = page.extract_text() or ""
            pages.append(page_text)
        return pages, len(reader.pages), ""
    except Exception as exc:
        return [], 0, str(exc)


def _extract_with_pymupdf(source: Path) -> tuple[list[str], int, str]:
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return [], 0, "PyMuPDF não instalado"

    try:
        document = fitz.open(str(source))
        try:
            # sort=True preserva a ordem visual das colunas da lista de material.
            pages = [page.get_text("text", sort=True) or "" for page in document]
            return pages, len(document), ""
        finally:
            document.close()
    except Exception as exc:
        return [], 0, str(exc)


def parse_material_pdf(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    if not source.exists() or not source.is_file():
        raise ValueError("Arquivo PDF não encontrado.")
    if source.suffix.lower() != ".pdf":
        raise ValueError("Selecione um arquivo PDF.")

    pages, page_count, pypdf_error = _extract_with_pypdf(source)
    parsed = parse_material_text(_material_pages_text(pages), source.name)

    # Alguns PDFs gerados/mesclados pelo iText possuem texto pesquisável, mas o
    # pypdf devolve páginas vazias. Nesses casos o PyMuPDF consegue ler a mesma
    # tabela sem OCR e mantém o processo rápido.
    if not parsed["assemblies"]:
        fallback_pages, fallback_count, pymupdf_error = _extract_with_pymupdf(source)
        fallback = parse_material_text(_material_pages_text(fallback_pages), source.name)
        if fallback["assemblies"]:
            parsed = fallback
            pages = fallback_pages
            page_count = fallback_count
        elif not pages and not fallback_pages:
            details = "; ".join(
                value for value in (pypdf_error, pymupdf_error) if value
            )
            raise ValueError(
                "Não foi possível ler o PDF. Execute 'Instalar dependencias' e tente novamente."
                + (f" Detalhes: {details}" if details else "")
            )

    parsed["page_count"] = page_count
    if not parsed["assemblies"]:
        raise ValueError(
            "Não foram encontrados conjuntos na lista de material. "
            "Verifique se o PDF possui texto pesquisável e uma lista de material compatível."
        )
    parsed["source_path"] = str(source)
    parsed["source_name"] = source.name
    return parsed
