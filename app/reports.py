from __future__ import annotations

import csv
import html
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as xml_escape

from .paths import EXPORT_DIR, ensure_folders
from .utils import utcish_file_stamp


REPORTS = {
    "overview": ("Andamento geral", "Visão consolidada de etapas, prazos, horas e indicadores importados dos projetos."),
    "hours": ("Horas por projeto", "Comparativo de horas registradas, peso detalhado e produtividade dos projetos."),
    "issues": ("Pendências", "Pendências abertas, responsáveis, vencimentos e impacto nos projetos."),
    "completed": ("Projetos concluídos", "Histórico de conclusão com prazos, horas e indicadores de detalhamento."),
}


def export_projects_csv(projects: list[dict[str, Any]]) -> Path:
    ensure_folders()
    path = EXPORT_DIR / f"projetos_{utcish_file_stamp()}.csv"
    fields = ["code", "title", "client", "responsible", "status", "progress", "hours", "current_stage", "due_date"]
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore", delimiter=";")
        writer.writeheader()
        for project in projects:
            writer.writerow(project)
    return path


def export_activity_csv(activity: list[dict[str, Any]]) -> Path:
    ensure_folders()
    path = EXPORT_DIR / f"historico_{utcish_file_stamp()}.csv"
    fields = ["created_at", "entity", "action", "message", "project_id"]
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore", delimiter=";")
        writer.writeheader()
        for item in activity:
            writer.writerow(item)
    return path


def export_report(projects: list[dict[str, Any]], activity: list[dict[str, Any]], report_type: str, output_format: str) -> Path:
    ensure_folders()
    report_type = report_type if report_type in REPORTS else "overview"
    output_format = "excel" if str(output_format).lower() == "excel" else "html"
    payload = _report_payload(projects, activity, report_type)
    if output_format == "excel":
        return _write_xlsx(payload, report_type)
    return _write_html(payload, report_type)


def _report_payload(projects: list[dict[str, Any]], activity: list[dict[str, Any]], report_type: str) -> dict[str, Any]:
    title, description = REPORTS[report_type]
    rows = _report_rows(projects, activity, report_type)
    return {
        "title": title,
        "description": description,
        "generated_at": datetime.now().strftime("%d-%m-%Y / %H:%M"),
        "summary": _summary(projects, rows, report_type),
        "insights": _insights(projects, rows, report_type),
        "headers": _headers(report_type),
        "rows": rows,
    }


def _headers(report_type: str) -> list[str]:
    return {
        "hours": [
            "Projeto", "Rev.", "Título", "Responsável", "Status", "Início", "Conclusão",
            "Horas no projeto", "Peso total", "Conjuntos", "Produtividade", "Etapa atual",
        ],
        "issues": [
            "Projeto", "Rev.", "Pendência", "Prioridade", "Status", "Responsável", "Prazo",
            "Descrição", "Peso do projeto", "Conjuntos",
        ],
        "completed": [
            "Projeto", "Rev.", "Título", "Responsável", "Início", "Conclusão", "Prazo",
            "Folhas", "Peso total", "Conjuntos", "Horas", "Produtividade", "Atualização",
        ],
        "overview": [
            "Projeto", "Rev.", "Título", "Cliente", "Status", "Prioridade", "Progresso",
            "Etapa atual", "Início", "Conclusão", "Prazo", "Folhas", "Peso total",
            "Conjuntos", "Horas", "Pendências",
        ],
    }[report_type]


def _report_rows(projects: list[dict[str, Any]], activity: list[dict[str, Any]], report_type: str) -> list[list[Any]]:
    if report_type == "hours":
        return [
            [
                project.get("code") or "-",
                _revision(project),
                project.get("title") or "-",
                project.get("responsible") or "-",
                project.get("status") or "-",
                _format_date_only(project.get("start_date")),
                _completion_date(project),
                _format_seconds(_seconds(project)),
                _format_weight(_weight(project)),
                _assemblies(project),
                _format_productivity(project),
                _stage_name(project.get("current_stage")),
            ]
            for project in projects
        ]
    if report_type == "issues":
        rows: list[list[Any]] = []
        for project in projects:
            for issue in project.get("issues", []) or []:
                if str(issue.get("status") or "").lower() == "resolvida":
                    continue
                rows.append(
                    [
                        project.get("code") or "-",
                        _revision(project),
                        issue.get("title") or "-",
                        issue.get("priority") or "-",
                        issue.get("status") or "-",
                        issue.get("responsible") or "-",
                        _format_date_only(issue.get("due_date")),
                        issue.get("description") or "",
                        _format_weight(_weight(project)),
                        _assemblies(project),
                    ]
                )
        return rows
    if report_type == "completed":
        return [
            [
                project.get("code") or "-",
                _revision(project),
                project.get("title") or "-",
                project.get("responsible") or "-",
                _format_date_only(project.get("start_date")),
                _completion_date(project),
                _format_date_only(project.get("due_date")),
                _pages(project),
                _format_weight(_weight(project)),
                _assemblies(project),
                _format_seconds(_seconds(project)),
                _format_productivity(project),
                _format_datetime(project.get("updated_at")),
            ]
            for project in projects
            if _is_completed(project)
        ]
    return [
        [
            project.get("code") or "-",
            _revision(project),
            project.get("title") or "-",
            project.get("client") or "-",
            project.get("status") or "-",
            project.get("priority") or "-",
            _format_percent(project.get("progress")),
            _stage_name(project.get("current_stage")),
            _format_date_only(project.get("start_date")),
            _completion_date(project),
            _format_date_only(project.get("due_date")),
            _pages(project),
            _format_weight(_weight(project)),
            _assemblies(project),
            _format_seconds(_seconds(project)),
            _issue_count(project),
        ]
        for project in projects
    ]


def _summary(projects: list[dict[str, Any]], rows: list[list[Any]], report_type: str) -> list[tuple[str, Any]]:
    completed_projects = [project for project in projects if _is_completed(project)]
    active_projects = [project for project in projects if not _is_completed(project) and str(project.get("status") or "") != "Arquivado"]
    imported = [project for project in projects if _has_indicators(project)]
    total_seconds = sum(_seconds(project) for project in projects)
    total_weight = sum(_weight(project) for project in imported)
    total_assemblies = sum(_assemblies(project) for project in imported)
    total_pages = sum(_pages(project) for project in imported)

    if report_type == "hours":
        hours_projects = [project for project in projects if _seconds(project) > 0]
        avg_seconds = int(total_seconds / len(hours_projects)) if hours_projects else 0
        productivity = (total_weight / (total_seconds / 3600.0)) if total_weight > 0 and total_seconds > 0 else 0
        return [
            ("Projetos com horas", len(hours_projects)),
            ("Horas totais", _format_seconds(total_seconds)),
            ("Média por projeto", _format_seconds(avg_seconds)),
            ("Peso importado", _format_weight(total_weight)),
            ("Conjuntos", total_assemblies),
            ("Produtividade geral", f"{_number(productivity)} kg/h" if productivity else "—"),
        ]
    if report_type == "issues":
        open_issues = _open_issues(projects)
        overdue = sum(1 for _, issue in open_issues if _is_date_overdue(issue.get("due_date")))
        high = sum(1 for _, issue in open_issues if "alta" in str(issue.get("priority") or "").lower())
        affected_ids = {int(project.get("id") or 0) for project, _ in open_issues}
        affected = [project for project in projects if int(project.get("id") or 0) in affected_ids]
        return [
            ("Pendências abertas", len(open_issues)),
            ("Vencidas", overdue),
            ("Prioridade alta", high),
            ("Projetos afetados", len(affected)),
            ("Peso afetado", _format_weight(sum(_weight(project) for project in affected if _has_indicators(project)))),
            ("Conjuntos afetados", sum(_assemblies(project) for project in affected if _has_indicators(project))),
        ]
    if report_type == "completed":
        completed_seconds = sum(_seconds(project) for project in completed_projects)
        completed_weight = sum(_weight(project) for project in completed_projects if _has_indicators(project))
        return [
            ("Projetos concluídos", len(completed_projects)),
            ("Peso detalhado", _format_weight(completed_weight)),
            ("Conjuntos", sum(_assemblies(project) for project in completed_projects if _has_indicators(project))),
            ("Folhas", sum(_pages(project) for project in completed_projects if _has_indicators(project))),
            ("Horas totais", _format_seconds(completed_seconds)),
            ("Registros listados", len(rows)),
        ]
    return [
        ("Projetos", len(projects)),
        ("Em andamento", len(active_projects)),
        ("Concluídos", len(completed_projects)),
        ("Horas totais", _format_seconds(total_seconds)),
        ("Peso importado", _format_weight(total_weight)),
        ("Conjuntos", total_assemblies),
    ]


def _insights(projects: list[dict[str, Any]], rows: list[list[Any]], report_type: str) -> list[str]:
    if not projects:
        return ["Nenhum projeto cadastrado no período do relatório."]
    imported = [project for project in projects if _has_indicators(project)]

    if report_type == "issues":
        open_issues = _open_issues(projects)
        if not open_issues:
            return ["Não há pendências abertas nos projetos listados."]
        overdue = [(project, issue) for project, issue in open_issues if _is_date_overdue(issue.get("due_date"))]
        insights = [f"{len(open_issues)} pendência(s) aberta(s) em {len({int(p.get('id') or 0) for p, _ in open_issues})} projeto(s)."]
        if overdue:
            insights.append(f"{len(overdue)} pendência(s) está(ão) com o prazo vencido.")
        affected = {int(project.get("id") or 0) for project, _ in open_issues}
        affected_imported = [project for project in imported if int(project.get("id") or 0) in affected]
        if affected_imported:
            insights.append(
                f"Os projetos afetados com PDF importado representam {_format_weight(sum(_weight(project) for project in affected_imported))} e "
                f"{sum(_assemblies(project) for project in affected_imported)} conjunto(s)."
            )
        return insights

    if report_type == "hours":
        timed = [project for project in projects if _seconds(project) > 0]
        if not timed:
            return ["Ainda não há horas registradas nos projetos listados."]
        total_seconds = sum(_seconds(project) for project in timed)
        longest = max(timed, key=_seconds)
        insights = [
            f"Foram registradas {_format_seconds(total_seconds)} em {len(timed)} projeto(s).",
            f"Maior tempo registrado: {longest.get('code') or '-'} com {_format_seconds(_seconds(longest))}.",
        ]
        productive = [project for project in imported if _seconds(project) > 0 and _weight(project) > 0]
        if productive:
            total_weight = sum(_weight(project) for project in productive)
            productivity = total_weight / (sum(_seconds(project) for project in productive) / 3600.0)
            best = max(productive, key=lambda project: _weight(project) / (_seconds(project) / 3600.0))
            insights.append(f"Produtividade consolidada dos projetos com PDF: {_number(productivity)} kg/h.")
            insights.append(f"Maior relação peso/tempo: {best.get('code') or '-'} com {_format_productivity(best)}.")
        else:
            insights.append("Importe os PDFs para relacionar horas, peso detalhado e produtividade.")
        return insights

    if report_type == "completed":
        completed_projects = [project for project in projects if _is_completed(project)]
        if not completed_projects:
            return ["Nenhum projeto concluído foi encontrado."]
        insights = [f"{len(completed_projects)} projeto(s) concluído(s) consta(m) no histórico."]
        completed_imported = [project for project in completed_projects if _has_indicators(project)]
        if completed_imported:
            insights.append(
                f"Os concluídos com PDF somam {_format_weight(sum(_weight(project) for project in completed_imported))}, "
                f"{sum(_assemblies(project) for project in completed_imported)} conjunto(s) e "
                f"{sum(_pages(project) for project in completed_imported)} folha(s)."
            )
            heaviest = max(completed_imported, key=_weight)
            insights.append(f"Maior peso concluído: {heaviest.get('code') or '-'} com {_format_weight(_weight(heaviest))}.")
        cycles = [_cycle_days(project) for project in completed_projects if _cycle_days(project) is not None]
        if cycles:
            insights.append(f"Tempo médio entre início e conclusão: {_number(sum(cycles) / len(cycles))} dia(s) corrido(s).")
        latest = max(completed_projects, key=lambda project: _parse_date(project.get("completed_at") or project.get("updated_at")) or datetime.min)
        insights.append(f"Conclusão mais recente: {latest.get('code') or '-'} em {_completion_date(latest)}.")
        return insights

    completed = sum(1 for project in projects if _is_completed(project))
    insights = [f"{completed} de {len(projects)} projeto(s) está(ão) concluído(s)."]
    if imported:
        weight = sum(_weight(project) for project in imported)
        assemblies = sum(_assemblies(project) for project in imported)
        pages = sum(_pages(project) for project in imported)
        insights.append(
            f"{len(imported)} projeto(s) possui(em) PDF importado, somando {_format_weight(weight)}, {assemblies} conjunto(s) e {pages} folha(s)."
        )
        heaviest = max(imported, key=_weight)
        insights.append(f"Maior peso registrado: {heaviest.get('code') or '-'} com {_format_weight(_weight(heaviest))}.")
    else:
        insights.append("Ainda não há indicadores de PDF importados para enriquecer a análise de peso e conjuntos.")
    delayed = [project for project in projects if not _is_completed(project) and _is_date_overdue(project.get("due_date"))]
    if delayed:
        insights.append(f"{len(delayed)} projeto(s) está(ão) com o prazo previsto vencido.")
    timed = [project for project in projects if _seconds(project) > 0]
    if timed:
        longest = max(timed, key=_seconds)
        insights.append(f"Maior tempo registrado: {longest.get('code') or '-'} com {_format_seconds(_seconds(longest))}.")
    return insights


def _write_html(payload: dict[str, Any], report_type: str) -> Path:
    path = EXPORT_DIR / f"relatorio_{report_type}_{utcish_file_stamp()}.html"
    cards = "\n".join(
        f"<div class='card'><span>{_h(label)}</span><strong>{_h(value)}</strong></div>"
        for label, value in payload["summary"]
    )
    insights = "".join(f"<li>{_h(item)}</li>" for item in payload.get("insights", []))
    headers = "".join(f"<th>{_h(header)}</th>" for header in payload["headers"])
    wrap_headers = {"Título", "Etapa atual", "Descrição", "Pendência"}
    rendered_rows = []
    for row in payload["rows"]:
        cells = []
        for header, value in zip(payload["headers"], row):
            cell_class = "wrap" if header in wrap_headers else "nowrap"
            cells.append(f"<td class='{cell_class}'>{_h(value)}</td>")
        rendered_rows.append("<tr>" + "".join(cells) + "</tr>")
    rows = "\n".join(rendered_rows)
    if not rows:
        rows = f"<tr><td colspan='{len(payload['headers'])}'>Nenhum registro encontrado para este relatório.</td></tr>"
    document = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="utf-8" />
<title>{_h(payload['title'])}</title>
<style>
  :root {{ --blue:#0078d4; --blue-soft:#eff6fc; --text:#323130; --muted:#605e5c; --line:#edebe9; --strong:#d2d0ce; --bg:#f3f2f1; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; font-family:"Segoe UI", Arial, sans-serif; background:var(--bg); color:var(--text); }}
  .page {{ width:calc(100% - 32px); max-width:1680px; margin:16px auto; background:#fff; border:1px solid var(--strong); box-shadow:0 1.6px 3.6px rgba(0,0,0,.13); }}
  header {{ padding:24px 28px 18px; border-left:4px solid var(--blue); border-bottom:1px solid var(--line); }}
  h1 {{ margin:0; font-size:24px; font-weight:600; }}
  .desc {{ margin-top:6px; color:var(--muted); }}
  .generated {{ margin-top:14px; font-size:12px; color:var(--muted); }}
  .cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:1px; background:var(--line); border-bottom:1px solid var(--line); }}
  .card {{ background:#fff; padding:16px 18px; min-height:78px; }}
  .card span {{ display:block; color:var(--muted); font-size:11px; text-transform:uppercase; }}
  .card strong {{ display:block; margin-top:7px; font-size:20px; }}
  .insights {{ padding:16px 28px; border-bottom:1px solid var(--line); background:#fafcff; }}
  .insights h2 {{ margin:0 0 8px; font-size:14px; font-weight:600; }}
  .insights ul {{ margin:0; padding-left:18px; color:var(--muted); font-size:12px; line-height:1.7; }}
  .table-wrap {{ width:100%; overflow:auto; }}
  table {{ width:100%; min-width:{max(1040, len(payload['headers']) * 92)}px; border-collapse:collapse; }}
  th {{ height:40px; padding:0 11px; background:#faf9f8; color:var(--muted); font-size:10.5px; text-align:left; border-bottom:1px solid var(--strong); white-space:nowrap; }}
  td {{ min-height:40px; padding:9px 11px; border-bottom:1px solid var(--line); font-size:11.5px; vertical-align:top; }}
  td.nowrap {{ white-space:nowrap; }}
  td.wrap {{ white-space:normal; min-width:150px; line-height:1.35; }}
  tr:hover td {{ background:#faf9f8; }}
  footer {{ padding:14px 28px; color:var(--muted); font-size:11px; }}
  @media print {{ body {{ background:#fff; }} .page {{ width:100%; margin:0; border:0; box-shadow:none; max-width:none; }} .table-wrap {{ overflow:visible; }} table {{ min-width:0; font-size:9px; }} th,td {{ padding:5px; }} }}
</style>
</head>
<body>
<main class="page">
  <header>
    <h1>{_h(payload['title'])}</h1>
    <div class="desc">{_h(payload['description'])}</div>
    <div class="generated">Gerado em {payload['generated_at']}</div>
  </header>
  <section class="cards">{cards}</section>
  <section class="insights"><h2>Destaques</h2><ul>{insights}</ul></section>
  <div class="table-wrap">
    <table>
      <thead><tr>{headers}</tr></thead>
      <tbody>{rows}</tbody>
    </table>
  </div>
  <footer>Controle de Projetos — Relatório em HTML</footer>
</main>
</body>
</html>
"""
    path.write_text(document, encoding="utf-8")
    return path


def _write_xlsx(payload: dict[str, Any], report_type: str) -> Path:
    path = EXPORT_DIR / f"relatorio_{report_type}_{utcish_file_stamp()}.xlsx"
    rows: list[list[Any]] = [
        [payload["title"]],
        [payload["description"]],
        [f"Gerado em {payload['generated_at']}"],
        [],
    ]
    rows.extend([[label, value] for label, value in payload["summary"]])
    rows.append([])
    rows.append(["Destaques"])
    rows.extend([[item] for item in payload.get("insights", [])])
    rows.append([])
    rows.append(payload["headers"])
    rows.extend(payload["rows"])
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as package:
        package.writestr("[Content_Types].xml", _xlsx_content_types())
        package.writestr("_rels/.rels", _xlsx_root_rels())
        package.writestr("xl/workbook.xml", _xlsx_workbook())
        package.writestr("xl/_rels/workbook.xml.rels", _xlsx_workbook_rels())
        package.writestr("xl/styles.xml", _xlsx_styles())
        package.writestr("xl/worksheets/sheet1.xml", _xlsx_sheet(rows))
    return path


def _xlsx_sheet(rows: list[list[Any]]) -> str:
    row_xml = []
    for row_index, row in enumerate(rows, 1):
        cells = []
        for col_index, value in enumerate(row, 1):
            ref = f"{_column_name(col_index)}{row_index}"
            cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{xml_escape(str(value))}</t></is></c>')
        row_xml.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetViews><sheetView workbookViewId="0"/></sheetViews>'
        '<sheetFormatPr defaultRowHeight="18"/>'
        f'<sheetData>{"".join(row_xml)}</sheetData>'
        '</worksheet>'
    )


def _xlsx_content_types() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>"""


def _xlsx_root_rels() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""


def _xlsx_workbook() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="Relatório" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""


def _xlsx_workbook_rels() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""


def _xlsx_styles() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="1"><font><sz val="11"/><name val="Segoe UI"/></font></fonts>
<fills count="1"><fill><patternFill patternType="none"/></fill></fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>
</styleSheet>"""


def _column_name(index: int) -> str:
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def _indicator(project: dict[str, Any]) -> dict[str, Any]:
    value = project.get("indicators")
    return value if isinstance(value, dict) else {}


def _has_indicators(project: dict[str, Any]) -> bool:
    indicator = _indicator(project)
    return bool(
        project.get("indicators_registered")
        or indicator.get("imported_at")
        or float(indicator.get("total_weight") or project.get("indicator_total_weight") or 0) > 0
        or int(indicator.get("assembly_count") or project.get("indicator_assembly_count") or 0) > 0
    )


def _weight(project: dict[str, Any]) -> float:
    indicator = _indicator(project)
    return max(0.0, float(indicator.get("total_weight") or project.get("indicator_total_weight") or 0))


def _assemblies(project: dict[str, Any]) -> int:
    indicator = _indicator(project)
    return max(0, int(indicator.get("assembly_count") or project.get("indicator_assembly_count") or 0))


def _pages(project: dict[str, Any]) -> int:
    indicator = _indicator(project)
    return max(0, int(
        indicator.get("page_count")
        or project.get("indicator_page_count")
        or project.get("sheet_total")
        or project.get("sheet_count")
        or 0
    ))


def _seconds(project: dict[str, Any]) -> int:
    return max(0, int(float(project.get("active_seconds") or 0)))


def _revision(project: dict[str, Any]) -> str:
    value = str(project.get("revision") or "0").strip() or "0"
    return f"Rev.{value}"


def _format_seconds(value: Any) -> str:
    seconds = max(0, int(float(value or 0)))
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    return f"{hours}h {minutes:02d}min"


def _format_percent(value: Any) -> str:
    try:
        return f"{float(value or 0):.0f}%"
    except (TypeError, ValueError):
        return "0%"


def _parse_date(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        pass
    for pattern in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:10], pattern)
        except ValueError:
            continue
    return None


def _format_date_only(value: Any) -> str:
    parsed = _parse_date(value)
    return parsed.strftime("%d/%m/%Y") if parsed else "-"


def _format_datetime(value: Any) -> str:
    parsed = _parse_date(value)
    return parsed.strftime("%d/%m/%Y / %H:%M") if parsed else "-"


def _completion_date(project: dict[str, Any]) -> str:
    if not _is_completed(project):
        return "-"
    return _format_date_only(project.get("completed_at") or project.get("updated_at"))


def _format_weight(value: Any) -> str:
    amount = max(0.0, float(value or 0))
    if amount <= 0:
        return "—"
    return f"{amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") + " kg"


def _number(value: Any) -> str:
    amount = float(value or 0)
    return f"{amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _format_productivity(project: dict[str, Any]) -> str:
    weight = _weight(project)
    seconds = _seconds(project)
    if weight <= 0 or seconds <= 0:
        return "—"
    return f"{_number(weight / (seconds / 3600.0))} kg/h"


def _stage_name(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or "-")
    return str(value or "-")


def _issue_count(project: dict[str, Any]) -> int:
    issues = project.get("issues")
    if isinstance(issues, list):
        return sum(1 for issue in issues if str(issue.get("status") or "").lower() != "resolvida")
    return int(project.get("issue_count") or 0)


def _open_issues(projects: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    result: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for project in projects:
        for issue in project.get("issues", []) or []:
            if str(issue.get("status") or "").lower() != "resolvida":
                result.append((project, issue))
    return result


def _is_date_overdue(value: Any) -> bool:
    parsed = _parse_date(value)
    return bool(parsed and parsed.date() < date.today())


def _cycle_days(project: dict[str, Any]) -> int | None:
    start = _parse_date(project.get("start_date"))
    end = _parse_date(project.get("completed_at") or project.get("updated_at")) if _is_completed(project) else None
    if not start or not end or end.date() < start.date():
        return None
    return (end.date() - start.date()).days + 1


def _is_completed(project: dict[str, Any]) -> bool:
    return str(project.get("status") or "") == "Concluído" or float(project.get("progress") or 0) >= 100


def _h(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)
