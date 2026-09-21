"""Preenchimento da planilha de rede "Controle Interno de Tramitação".

Usa automação do Excel (COM / pywin32) para acrescentar linhas SEM reescrever o
arquivo por fora — assim a formatação (logo, cores, formatação condicional) é
preservada e funciona mesmo com a planilha aberta.

Mapeamento das colunas (aba DADOS), preenchidas pelo programa: B..L.
As colunas M..U ficam vazias (preenchidas depois por outros setores).
"""
from __future__ import annotations

import os
import re
from datetime import date, datetime
from typing import Any

# Colunas (1-based) da tabela na aba DADOS.
COL_DATA = 2          # B
COL_OS = 3            # C  (dropdown: catálogo em W)
COL_CLIENTE = 4       # D
COL_TIPO = 5          # E  (DET. NOVO / REVISÃO / CANCELADO)
COL_DESENHO = 6       # F
COL_REV = 7           # G
COL_PROJ = 8          # H
COL_DATA_EMAIL = 9    # I
COL_LANC_MRJ = 10     # J
COL_LANC_LM = 11      # K
COL_LANC_IEIS = 12    # L
COL_OS_CATALOG = 23   # W (lista de validação das OS)
COL_CLIENT_CATALOG = 24  # X (descrição/cliente padrão da OS)

SHEET_NAME = "DADOS"
MAX_HEADER_SCAN = 40
MAX_CATALOG_SCAN = 400
DATE_FORMAT = "dd/mm/yyyy"
DATE_FORMAT_LOCAL = "dd/mm/aaaa"
EXCEL_EPOCH = date(1899, 12, 30)  # dia 0 do Excel (com o bug de 1900)
DATA_COLUMNS = tuple(range(COL_DATA, COL_LANC_IEIS + 1))
MAX_BLANK_GAP_AFTER_DATA = 50
MAX_ROW_SCAN = 50000
MAX_FORMULA_SEARCH = 200
CELL_ROW_RE = re.compile(r"(\$?[A-Z]{1,3}\$?)(\d+)")


class SpreadsheetError(Exception):
    """Erro ao acessar/gravar a planilha de tramitação."""


def _excel_serial(value: Any) -> int | None:
    """Converte data/datetime no número de série do Excel (sem hora, sem fuso)."""
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return (value - EXCEL_EPOCH).days
    return None


def _set_date_cell(sheet, row: int, col: int, value: Any) -> None:
    serial = _excel_serial(value)
    cell = sheet.Cells(row, col)
    if serial is None:
        cell.Value = value
        return
    cell.Value = serial
    try:
        cell.NumberFormatLocal = DATE_FORMAT_LOCAL
    except Exception:
        cell.NumberFormat = "@"
        cell.Value = value.strftime("%d/%m/%Y") if isinstance(value, (date, datetime)) else value


def _find_header_row(sheet) -> int:
    for row in range(1, MAX_HEADER_SCAN + 1):
        value = sheet.Cells(row, COL_DATA).Value
        if value and str(value).strip().upper() == "DATA":
            return row
    raise SpreadsheetError(f"Não encontrei o cabeçalho da tabela na aba '{SHEET_NAME}'.")


def _last_data_row(sheet, header_row: int) -> int:
    # A planilha costuma ter formatação/validação até muitas linhas abaixo da
    # área realmente preenchida. Por isso a busca considera valores reais nas
    # colunas gravadas pelo programa e para após um bloco grande de linhas vazias.
    candidates = [header_row]
    for col in DATA_COLUMNS:
        try:
            candidates.append(sheet.Cells(sheet.Rows.Count, col).End(-4162).Row)  # xlUp
        except Exception:
            continue
    try:
        used = sheet.UsedRange
        candidates.append(int(used.Row) + int(used.Rows.Count) - 1)
    except Exception:
        pass

    scan_until = max(header_row, max(candidates))
    scan_until = min(scan_until, header_row + MAX_ROW_SCAN, sheet.Rows.Count)
    last_row = header_row
    blank_gap = 0
    for row in range(header_row + 1, scan_until + 1):
        if _row_has_data(sheet, row):
            last_row = row
            blank_gap = 0
            continue
        if last_row > header_row:
            blank_gap += 1
            if blank_gap >= MAX_BLANK_GAP_AFTER_DATA:
                break
    return last_row


def _row_has_data(sheet, row: int) -> bool:
    for col in DATA_COLUMNS:
        value = sheet.Cells(row, col).Value
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return True
    return False


def _os_catalog(sheet, header_row: int) -> dict[str, str]:
    catalog: dict[str, str] = {}
    for row in range(header_row, header_row + MAX_CATALOG_SCAN):
        value = sheet.Cells(row, COL_OS_CATALOG).Value
        if value not in (None, ""):
            client = sheet.Cells(row, COL_CLIENT_CATALOG).Value
            catalog[str(value).strip()] = str(client or "").strip()
    return catalog


def _cell_formula(cell, *attributes: str) -> tuple[str, str]:
    for attribute in attributes:
        try:
            value = getattr(cell, attribute)
        except Exception:
            continue
        text = str(value or "").strip()
        if text.startswith("="):
            return attribute, text
    return "", ""


def _has_formula(cell) -> bool:
    try:
        return bool(getattr(cell, "HasFormula"))
    except Exception:
        pass
    return bool(
        _cell_formula(
            cell,
            "FormulaR1C1Local",
            "FormulaR1C1",
            "FormulaLocal",
            "Formula",
        )[1]
    )


def _retarget_formula_row(formula: str, source_row: int, target_row: int) -> str:
    def replace(match: re.Match[str]) -> str:
        if int(match.group(2)) != source_row:
            return match.group(0)
        return f"{match.group(1)}{target_row}"

    return CELL_ROW_RE.sub(replace, formula)


def _copy_client_formula(sheet, source_row: int, target_row: int) -> bool:
    source = sheet.Cells(source_row, COL_CLIENTE)
    target = sheet.Cells(target_row, COL_CLIENTE)
    attribute, formula = _cell_formula(source, "FormulaR1C1Local", "FormulaR1C1")
    if formula:
        try:
            setattr(target, attribute, formula)
            return True
        except Exception:
            pass

    attribute, formula = _cell_formula(source, "FormulaLocal", "Formula")
    if formula:
        try:
            setattr(target, attribute, _retarget_formula_row(formula, source_row, target_row))
            return True
        except Exception:
            pass
    return False


def _nearest_client_formula_row(sheet, target_row: int, header_row: int) -> int | None:
    for offset in range(0, MAX_FORMULA_SEARCH + 1):
        for row in (target_row - offset, target_row + offset):
            if row <= header_row or row < 1 or row > sheet.Rows.Count:
                continue
            if _has_formula(sheet.Cells(row, COL_CLIENTE)):
                return row
    return None


def _set_client_cell(sheet, target_row: int, header_row: int, os_value: str, catalog: dict[str, str], fallback: str) -> None:
    target = sheet.Cells(target_row, COL_CLIENTE)
    if _has_formula(target):
        return

    formula_row = _nearest_client_formula_row(sheet, target_row, header_row)
    if formula_row and _copy_client_formula(sheet, formula_row, target_row):
        return

    catalog_client = catalog.get(os_value, "")
    target.Value = catalog_client or fallback


def _existing_drawings(sheet, header_row: int, last_row: int) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for row in range(header_row + 1, last_row + 1):
        drawing = sheet.Cells(row, COL_DESENHO).Value
        if drawing in (None, ""):
            continue
        rev = sheet.Cells(row, COL_REV).Value
        rev_txt = str(int(rev)) if isinstance(rev, float) and rev.is_integer() else str(rev).strip()
        pairs.add((str(drawing).strip().upper(), rev_txt))
    return pairs


def _open_excel():
    try:
        import win32com.client  # type: ignore
    except Exception as exc:  # pragma: no cover - depende do ambiente
        raise SpreadsheetError(
            "Integração com o Excel indisponível (pywin32 não instalado)."
        ) from exc
    created = False
    try:
        excel = win32com.client.GetActiveObject("Excel.Application")
    except Exception:
        try:
            excel = win32com.client.DispatchEx("Excel.Application")
            created = True
        except Exception as exc:
            raise SpreadsheetError("Não foi possível iniciar o Excel.") from exc
    return excel, created


def _open_workbook(excel, path: str):
    target = os.path.abspath(path)
    for wb in excel.Workbooks:
        try:
            if os.path.abspath(wb.FullName).lower() == target.lower():
                return wb, False
        except Exception:
            continue
    try:
        wb = excel.Workbooks.Open(target)
    except Exception as exc:
        raise SpreadsheetError(f"Não foi possível abrir a planilha: {path}") from exc
    return wb, True


def append_rows(spreadsheet_path: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Acrescenta linhas na aba DADOS. Melhor-esforço: preenche o possível e
    reporta avisos (OS fora do catálogo, linha duplicada) em vez de falhar."""
    if not spreadsheet_path or not str(spreadsheet_path).strip():
        raise SpreadsheetError("Caminho da planilha de controle não informado.")
    if not os.path.exists(spreadsheet_path):
        raise SpreadsheetError(f"Planilha não encontrada: {spreadsheet_path}")

    excel, created_app = _open_excel()
    wb = None
    opened_wb = False
    warnings: list[str] = []
    written = 0
    skipped: list[str] = []
    try:
        wb, opened_wb = _open_workbook(excel, spreadsheet_path)
        try:
            sheet = wb.Worksheets(SHEET_NAME)
        except Exception as exc:
            raise SpreadsheetError(f"Aba '{SHEET_NAME}' não encontrada na planilha.") from exc

        header_row = _find_header_row(sheet)
        last_row = _last_data_row(sheet, header_row)
        catalog = _os_catalog(sheet, header_row)
        existing = _existing_drawings(sheet, header_row, last_row)

        target_row = last_row + 1
        for row in rows:
            drawing = str(row.get("drawing") or "").strip()
            rev_txt = str(row.get("rev") if row.get("rev") not in (None, "") else "0").strip()
            key = (drawing.upper(), rev_txt)
            if key in existing:
                skipped.append(f"{drawing} rev {rev_txt} já existe na planilha (linha não duplicada).")
                continue
            os_value = str(row.get("os") or "").strip()
            if os_value and catalog and os_value not in catalog:
                warnings.append(
                    f"A OS {os_value} não está no catálogo da planilha; a linha foi preenchida, "
                    "mas verifique/cadastre a OS manualmente."
            )
            _set_date_cell(sheet, target_row, COL_DATA, row.get("data"))
            sheet.Cells(target_row, COL_OS).Value = os_value
            _set_client_cell(sheet, target_row, header_row, os_value, catalog, str(row.get("client") or "").strip())
            sheet.Cells(target_row, COL_TIPO).Value = str(row.get("tipo") or "").strip()
            sheet.Cells(target_row, COL_DESENHO).Value = drawing
            try:
                sheet.Cells(target_row, COL_REV).Value = int(float(rev_txt))
            except (TypeError, ValueError):
                sheet.Cells(target_row, COL_REV).Value = rev_txt
            sheet.Cells(target_row, COL_PROJ).Value = str(row.get("proj") or "").strip()
            _set_date_cell(sheet, target_row, COL_DATA_EMAIL, row.get("email_date"))
            sheet.Cells(target_row, COL_LANC_MRJ).Value = str(row.get("lanc_mrj") or "SIM").strip()
            sheet.Cells(target_row, COL_LANC_LM).Value = str(row.get("lanc_lm") or "SIM").strip()
            sheet.Cells(target_row, COL_LANC_IEIS).Value = str(row.get("lanc_ieis") or "SIM").strip()
            existing.add(key)
            target_row += 1
            written += 1

        if written:
            wb.Save()
    finally:
        # Fecha o que NÓS abrimos (já salvo); deixa intacto o que já estava aberto.
        try:
            if wb is not None and opened_wb:
                wb.Close(SaveChanges=False)
        except Exception:
            pass
        try:
            if created_app:
                excel.Quit()
        except Exception:
            pass

    return {"written": written, "warnings": warnings, "skipped": skipped, "sheet": SHEET_NAME}
