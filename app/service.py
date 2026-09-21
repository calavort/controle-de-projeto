from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .database import ControleDatabase
from .indicators import parse_material_pdf
from .paths import APP_DIR, LOG_DIR
from .reports import export_activity_csv, export_projects_csv, export_report
from .spreadsheet import SpreadsheetError, append_rows
from .utils import parse_int

PROGRESSO_DETALHAMENTO_ARG = "--progresso-detalhamento"


def _project_revision(project: dict[str, Any]) -> int:
    try:
        return int(float(str(project.get("revision") or "0").strip() or "0"))
    except (TypeError, ValueError):
        return 0


def build_tramitacao_row(project: dict[str, Any], projectist_abbreviation: str) -> dict[str, Any]:
    """Monta a linha da planilha de tramitação a partir do cadastro do projeto."""
    today = datetime.now()
    revision = _project_revision(project)
    client = str(project.get("client") or "").strip()
    area = str(project.get("area") or "").strip()
    cliente = " - ".join(part for part in (client, area) if part)
    return {
        "data": today,
        "os": str(project.get("os_number") or "").strip(),
        "client": cliente,
        "tipo": "DET. NOVO" if revision == 0 else "REVISÃO",
        "drawing": str(project.get("code") or "").strip(),
        "rev": revision,
        "proj": str(projectist_abbreviation or "").strip(),
        "email_date": today,
        "lanc_mrj": "SIM",
        "lanc_lm": "SIM",
        "lanc_ieis": "SIM",
    }


class ControleService:
    def __init__(self, db: ControleDatabase | None = None) -> None:
        self.db = db or ControleDatabase()

    def state(self) -> dict[str, Any]:
        return self.db.dashboard()

    def heartbeat_runtime(self) -> None:
        self.db.heartbeat_runtime()

    def create_project(self, data: dict[str, Any]) -> dict[str, Any]:
        return self.db.create_project(data)

    def reset_operational_data(self) -> dict[str, Any]:
        return self.db.reset_operational_data()

    def replace_project_stages(self, project_id: int, stages: list[dict[str, Any]]) -> dict[str, Any]:
        return self.db.replace_project_stages(project_id, stages)

    def get_stage_template(self, program: str, mode: str) -> dict[str, Any]:
        return self.db.get_stage_template(program, mode)

    def replace_stage_template(self, program: str, mode: str, stages: list[dict[str, Any]]) -> dict[str, Any]:
        return self.db.replace_stage_template(program, mode, stages)

    def finalize_project(self, project_id: int) -> dict[str, Any]:
        """Conclui o projeto e preenche a planilha de tramitação (melhor-esforço)."""
        project = self.db.get_project(project_id)
        if not project:
            raise ValueError("Projeto não encontrado.")

        self.db.set_project_status(project_id, "Concluído")

        abbreviation = self.db.setting("projectist_abbreviation", "") or ""
        row = build_tramitacao_row(project, abbreviation)
        spreadsheet_path = str(project.get("spreadsheet_path") or "").strip()

        report: dict[str, Any] = {
            "attempted": False,
            "written": 0,
            "warnings": [],
            "skipped": [],
            "error": None,
        }
        if not spreadsheet_path:
            report["error"] = "Planilha de controle não configurada no projeto (cadastre o caminho em 'Planilha de controle')."
        else:
            report["attempted"] = True
            try:
                result = append_rows(spreadsheet_path, [row])
                report["written"] = int(result.get("written") or 0)
                report["warnings"] = list(result.get("warnings") or [])
                report["skipped"] = list(result.get("skipped") or [])
            except SpreadsheetError as exc:
                report["error"] = str(exc)
            except Exception as exc:  # pragma: no cover - proteção extra
                report["error"] = f"Falha inesperada ao gravar a planilha: {exc}"

        return {"project": self.db.get_project(project_id), "spreadsheet": report, "row": row}

    def update_project(self, project_id: int, data: dict[str, Any]) -> dict[str, Any]:
        return self.db.update_project(project_id, data)

    def import_project_indicators(self, project_id: int, pdf_path: str, confirm: bool = False) -> dict[str, Any]:
        project = self.db.get_project(project_id)
        if not project:
            raise ValueError("Projeto não encontrado.")

        parsed = parse_material_pdf(pdf_path)
        expected_code = str(project.get("code") or "").strip().upper()
        identified_code = str(parsed.get("drawing_code") or "").strip().upper()
        code_matches = bool(identified_code and expected_code and identified_code == expected_code)
        parsed["expected_drawing_code"] = expected_code
        parsed["project_code_match"] = code_matches

        if confirm and not identified_code:
            raise ValueError(
                "Não foi possível identificar o código do desenho no PDF. "
                "Use a edição manual para evitar associar o arquivo ao projeto errado."
            )
        if confirm and not code_matches:
            raise ValueError(
                f"O PDF pertence ao desenho {identified_code}, mas o projeto selecionado é {expected_code}. "
                "A importação foi cancelada."
            )

        saved = self.db.save_project_indicators(project_id, parsed, source_type="pdf") if confirm else None
        return {"parsed": parsed, "indicators": saved, "saved": bool(saved)}

    def save_project_indicators(self, project_id: int, data: dict[str, Any]) -> dict[str, Any]:
        return self.db.save_project_indicators(project_id, data, source_type="manual")

    def delete_project(self, project_id: int) -> None:
        self.db.delete_project(project_id)

    def set_active_project(self, project_id: int) -> dict[str, Any]:
        return self.db.set_project_active(project_id)

    def set_project_status(self, project_id: int, status: str) -> dict[str, Any]:
        return self.db.set_project_status(project_id, status)

    def raise_project_revision(
        self, project_id: int, revision: str, hours: float = 0, minutes: float = 0, revision_date: str = ""
    ) -> dict[str, Any]:
        seconds = max(0, int((float(hours or 0) * 3600) + (float(minutes or 0) * 60)))
        return self.db.raise_project_revision(project_id, revision, seconds, revision_date)

    def update_settings(self, data: dict[str, Any]) -> dict[str, Any]:
        for key in (
            "user_name",
            "user_nickname",
            "projectist_name",
            "projectist_abbreviation",
        ):
            if key in data:
                self.db.set_setting(key, str(data[key] or "").strip())
        if "report_output_format" in data:
            value = str(data.get("report_output_format") or "html").strip().lower()
            self.db.set_setting("report_output_format", "excel" if value == "excel" else "html")
        if "auto_description_enabled" in data:
            value = str(data.get("auto_description_enabled") or "0").strip().lower()
            enabled = value not in {"0", "false", "não", "nao", "off"}
            self.db.set_setting("auto_description_enabled", "1" if enabled else "0")
        if "project_statuses" in data:
            raw_statuses = data.get("project_statuses")
            if isinstance(raw_statuses, str):
                raw_statuses = [item.strip() for item in raw_statuses.split("|")]
            clean_statuses: list[str] = []
            for item in raw_statuses or []:
                value = str(item or "").strip()
                if value and value.casefold() not in {existing.casefold() for existing in clean_statuses}:
                    clean_statuses.append(value)
            # Status essenciais do fluxo permanecem disponíveis; os demais são livres.
            for required in ("Em andamento", "Concluído"):
                if required.casefold() not in {item.casefold() for item in clean_statuses}:
                    clean_statuses.append(required)
            self.db.set_setting("project_statuses", __import__("json").dumps(clean_statuses, ensure_ascii=False))
        return self.state()["settings"]

    def open_path(self, path: str, folder: bool = False) -> dict[str, Any]:
        target = Path(path)
        if folder:
            target = target if target.is_dir() else target.parent
        if not target.exists():
            raise ValueError("Caminho não encontrado.")
        if os.name == "nt":
            os.startfile(str(target))  # type: ignore[attr-defined]
        else:
            raise ValueError("Abertura automática de caminho está disponível apenas no Windows.")
        return {"ok": True}

    def open_progresso_detalhamento(self) -> dict[str, Any]:
        tool_dir = APP_DIR / "progresso_detalhamento"
        script_path = tool_dir / "Progresso de detalhamento.py"
        running_from_bundle = bool(getattr(sys, "frozen", False))
        if not running_from_bundle and not script_path.is_file():
            raise ValueError("Progresso de detalhamento incorporado nao foi encontrado.")

        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_path = LOG_DIR / "progresso_detalhamento.log"
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        command = (
            [sys.executable, PROGRESSO_DETALHAMENTO_ARG]
            if running_from_bundle
            else [sys.executable, str(script_path)]
        )
        cwd = tool_dir if tool_dir.is_dir() else Path(sys.executable).resolve().parent

        try:
            with log_path.open("a", encoding="utf-8") as log_file:
                log_file.write(
                    f"\n[{datetime.now().isoformat(timespec='seconds')}] Abrindo Progresso de detalhamento: "
                    f"{command} (cwd={cwd})\n"
                )
                log_file.flush()
                process = subprocess.Popen(
                    command,
                    cwd=str(cwd),
                    stdin=subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    creationflags=creationflags,
                )
        except Exception as exc:
            raise ValueError(f"Nao foi possivel abrir o Progresso de detalhamento: {exc}") from exc

        time.sleep(0.75)
        exit_code = process.poll()
        if exit_code is not None:
            tail = self._read_log_tail(log_path)
            message = f"Progresso de detalhamento encerrou ao abrir (codigo {exit_code})."
            if tail:
                message = f"{message} Ultimas linhas do log: {tail}"
            raise ValueError(message)

        return {"pid": process.pid}

    @staticmethod
    def _read_log_tail(path: Path, limit: int = 1800) -> str:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return text[-limit:].strip()

    def choose_path(self, folder: bool = False, initial_dir: str = "") -> dict[str, Any]:
        if os.name != "nt":
            raise ValueError("Seleção de caminho está disponível apenas no Windows.")
        try:
            import tkinter as tk
            from tkinter import filedialog
        except Exception as exc:
            raise ValueError(f"Não foi possível abrir o seletor de arquivos: {exc}") from exc

        initial_path = Path(initial_dir) if initial_dir else Path.home()
        if initial_path.is_file():
            initial_path = initial_path.parent
        if not initial_path.exists():
            initial_path = Path.home()

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        root.update()
        try:
            if folder:
                selected = filedialog.askdirectory(
                    title="Selecionar pasta",
                    initialdir=str(initial_path),
                    parent=root,
                )
            else:
                selected = filedialog.askopenfilename(
                    title="Selecionar arquivo",
                    initialdir=str(initial_path),
                    filetypes=[("Todos os arquivos", "*.*")],
                    parent=root,
                )
        finally:
            root.destroy()

        if not selected:
            return {"path": ""}
        selected_path = Path(selected)
        return {
            "path": str(selected_path),
            "name": selected_path.name,
            "file_type": "Pasta" if selected_path.is_dir() else selected_path.suffix.lstrip(".").upper(),
        }

    def export_projects(self) -> Path:
        return export_projects_csv(self.db.list_projects())

    def export_activity(self) -> Path:
        return export_activity_csv(self.db.activity(None, 1000))

    def export_report(self, report_type: str) -> Path:
        projects = []
        for project in self.db.list_projects():
            detailed = self.db.get_project(int(project["id"])) or project
            detailed.update({key: value for key, value in project.items() if key not in detailed})
            projects.append(detailed)
        output_format = self.db.setting("report_output_format", "html")
        return export_report(projects, self.db.activity(None, 1000), report_type, output_format)
