from __future__ import annotations

import json
import ntpath
import re
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .paths import APP_DIR, BACKUP_DIR, DB_PATH, ensure_folders
from .progress import (
    DEFAULT_PROGRAM,
    DEFAULT_STAGE_MODE,
    PROGRAMS,
    STAGE_MODES,
    calculate_project_progress,
    current_stage,
    default_template_stages,
    default_weights,
    normalize_weights,
    observed_weights_from_seconds,
    smooth_weights,
)
from .utils import clamp, from_json, now_iso, parse_float, parse_int, to_json, utcish_file_stamp
from .worktime import outside_schedule_seconds, project_worked_seconds


PROJECT_FIELDS = {
    "code",
    "os_number",
    "title",
    "client",
    "responsible",
    "projectist_name",
    "checker_name",
    "approver_name",
    "status",
    "priority",
    "revision",
    "program",
    "stage_mode",
    "discipline",
    "area",
    "description",
    "model_path",
    "model_name",
    "document_folder",
    "spreadsheet_path",
    "start_date",
    "due_date",
}

DETAIL_PROGRESS_SOURCE = "progresso_detalhamento"
REOPEN_STAGE_SOURCE = "manual_reopen"
TIME_DISTRIBUTION_SOURCE = "time_distribution"
DETAIL_PROGRESS_HISTORY = APP_DIR / "progresso_detalhamento" / "estatisticas_detalhamento.json"
DETAIL_STAGE_NAMES = {"detalhamento", "iniciar detalhamento"}

ISSUE_FIELDS = {"title", "description", "priority", "status", "responsible", "due_date", "drawing_id"}
FILE_FIELDS = {"name", "file_path", "file_type", "description"}
DRAWING_FIELDS = {"drawing_key", "code", "title", "revision", "drawing_type", "sheet_count", "view_count", "object_count", "status"}
SHEET_NUMBER_RE = re.compile(r"\[\s*(\d+)\s*\]")
TAG_NAME_RE = re.compile(r"^[A-Z0-9]+(?:[-_.][A-Z0-9]+){2,}$")


def is_multidrawing_type(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return text == "m" or "multi" in text or text.startswith("m|")


def sheet_number_from_text(*values: Any) -> int:
    for value in values:
        match = SHEET_NUMBER_RE.search(str(value or ""))
        if match:
            return int(match.group(1))
    return 0


class ControleDatabase:
    def __init__(self, db_path: Path | None = None) -> None:
        ensure_folders()
        self.db_path = db_path or DB_PATH
        self._lock = threading.RLock()
        self._runtime_project_id: int | None = None
        self._runtime_session_id: int | None = None
        self._runtime_last_heartbeat: datetime | None = None
        self._backup_existing_database()
        self.create_tables()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON;")
        connection.execute("PRAGMA busy_timeout = 5000;")
        return connection

    @contextmanager
    def session(self):
        connection = self.connect()
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def create_tables(self) -> None:
        with self._lock, self.session() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;

                CREATE TABLE IF NOT EXISTS projects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    os_number TEXT DEFAULT '',
                    title TEXT NOT NULL,
                    client TEXT DEFAULT '',
                    responsible TEXT DEFAULT '',
                    projectist_name TEXT DEFAULT '',
                    checker_name TEXT DEFAULT '',
                    approver_name TEXT DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'Em andamento',
                    priority TEXT NOT NULL DEFAULT 'Normal',
                    revision TEXT DEFAULT '0',
                    discipline TEXT DEFAULT '',
                    area TEXT DEFAULT '',
                    description TEXT DEFAULT '',
                    model_path TEXT DEFAULT '',
                    model_name TEXT DEFAULT '',
                    document_folder TEXT DEFAULT '',
                    spreadsheet_path TEXT DEFAULT '',
                    start_date TEXT DEFAULT '',
                    due_date TEXT DEFAULT '',
                    completed_at TEXT,
                    archived_at TEXT,
                    active INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS project_stages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER NOT NULL,
                    stage_order INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'Não iniciada',
                    internal_percent REAL NOT NULL DEFAULT 0,
                    source TEXT NOT NULL DEFAULT 'manual',
                    confidence REAL NOT NULL DEFAULT 0,
                    started_at TEXT,
                    last_activity_at TEXT,
                    completed_at TEXT,
                    active_seconds INTEGER NOT NULL DEFAULT 0,
                    time_adjustment_seconds INTEGER NOT NULL DEFAULT 0,
                    notes TEXT DEFAULT '',
                    manual_confirmed INTEGER NOT NULL DEFAULT 0,
                    weight REAL NOT NULL DEFAULT 0,
                    expected_total REAL DEFAULT 0,
                    observed_done REAL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(project_id, stage_order),
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS stage_templates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    program TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    stage_order INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    weight REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(program, mode, stage_order)
                );

                CREATE TABLE IF NOT EXISTS project_active_periods (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER NOT NULL,
                    start_at TEXT NOT NULL,
                    end_at TEXT,
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS app_presence (
                    day TEXT PRIMARY KEY
                );

                CREATE TABLE IF NOT EXISTS stage_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_stage_id INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    message TEXT NOT NULL,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(project_stage_id) REFERENCES project_stages(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS stage_time_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER NOT NULL,
                    stage_order INTEGER NOT NULL,
                    start_at TEXT,
                    end_at TEXT,
                    seconds INTEGER NOT NULL,
                    source TEXT NOT NULL DEFAULT 'manual',
                    note TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS drawings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER NOT NULL,
                    drawing_key TEXT DEFAULT '',
                    code TEXT DEFAULT '',
                    title TEXT DEFAULT '',
                    revision TEXT DEFAULT '',
                    drawing_type TEXT DEFAULT '',
                    sheet_count INTEGER NOT NULL DEFAULT 0,
                    view_count INTEGER NOT NULL DEFAULT 0,
                    object_count INTEGER NOT NULL DEFAULT 0,
                    last_seen_at TEXT,
                    status TEXT NOT NULL DEFAULT 'Em andamento',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS project_files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    file_type TEXT DEFAULT '',
                    description TEXT DEFAULT '',
                    size_bytes INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS issues (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER NOT NULL,
                    drawing_id INTEGER,
                    title TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    priority TEXT NOT NULL DEFAULT 'Média',
                    status TEXT NOT NULL DEFAULT 'Aberta',
                    responsible TEXT DEFAULT '',
                    due_date TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
                    FOREIGN KEY(drawing_id) REFERENCES drawings(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS work_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER NOT NULL,
                    started_at TEXT,
                    ended_at TEXT,
                    active_seconds INTEGER NOT NULL DEFAULT 0,
                    source TEXT NOT NULL DEFAULT 'manual',
                    note TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS project_revision_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER NOT NULL,
                    revision TEXT NOT NULL DEFAULT '',
                    snapshot_json TEXT NOT NULL DEFAULT '{}',
                    completed_at TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(project_id, revision),
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS activity_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER,
                    entity TEXT NOT NULL,
                    action TEXT NOT NULL,
                    message TEXT NOT NULL,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS stage_weight_profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    is_default INTEGER NOT NULL DEFAULT 0,
                    smoothing_factor REAL NOT NULL DEFAULT 0.30,
                    weights_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    archived_at TEXT
                );

                CREATE TABLE IF NOT EXISTS project_indicators (
                    project_id INTEGER PRIMARY KEY,
                    drawing_code TEXT DEFAULT '',
                    revision TEXT DEFAULT '',
                    total_weight REAL NOT NULL DEFAULT 0,
                    declared_weight REAL NOT NULL DEFAULT 0,
                    page_count INTEGER NOT NULL DEFAULT 0,
                    source_type TEXT NOT NULL DEFAULT 'manual',
                    source_path TEXT DEFAULT '',
                    source_name TEXT DEFAULT '',
                    imported_at TEXT,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS project_indicator_assemblies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER NOT NULL,
                    item_no TEXT NOT NULL DEFAULT '',
                    quantity INTEGER NOT NULL DEFAULT 1,
                    tag TEXT NOT NULL DEFAULT '',
                    unit_weight REAL NOT NULL DEFAULT 0,
                    weight REAL NOT NULL DEFAULT 0,
                    source_type TEXT NOT NULL DEFAULT 'manual',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS deleted_project_memory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL DEFAULT '',
                    model_path TEXT NOT NULL DEFAULT '',
                    model_name TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(code, model_path, model_name)
                );

                CREATE INDEX IF NOT EXISTS idx_projects_active ON projects(active);
                CREATE INDEX IF NOT EXISTS idx_project_stages_project ON project_stages(project_id, stage_order);
                CREATE INDEX IF NOT EXISTS idx_activity_project ON activity_history(project_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_indicator_assemblies_project ON project_indicator_assemblies(project_id, item_no);
                CREATE INDEX IF NOT EXISTS idx_indicator_assemblies_tag ON project_indicator_assemblies(project_id, tag);
                CREATE INDEX IF NOT EXISTS idx_deleted_project_memory_code ON deleted_project_memory(code);
                CREATE INDEX IF NOT EXISTS idx_deleted_project_memory_model_path ON deleted_project_memory(model_path);
                CREATE INDEX IF NOT EXISTS idx_deleted_project_memory_model_name ON deleted_project_memory(model_name);
                """
            )
            self._ensure_project_columns(connection)
            self._ensure_project_stage_columns(connection)
            self._ensure_revision_columns(connection)
            self._ensure_indicator_columns(connection)
            self._seed_defaults(connection)
            self._seed_stage_templates(connection)
            self._reconcile_existing_stage_flow_and_time(connection)
            self._repair_completed_project_metadata(connection)
            self._repair_inverted_completed_project_dates(connection)
            self._repair_project_43297_start_date(connection)
            self._seed_project_form_suggestions_from_projects(connection)
            self._seed_last_project_locations(connection)

    def _ensure_project_columns(self, connection: sqlite3.Connection) -> None:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(projects);").fetchall()}
        if "os_number" not in columns:
            connection.execute("ALTER TABLE projects ADD COLUMN os_number TEXT DEFAULT '';")
        if "projectist_name" not in columns:
            connection.execute("ALTER TABLE projects ADD COLUMN projectist_name TEXT DEFAULT '';")
            connection.execute(
                "UPDATE projects SET projectist_name = responsible WHERE projectist_name IS NULL OR projectist_name = '';"
            )
        if "checker_name" not in columns:
            connection.execute("ALTER TABLE projects ADD COLUMN checker_name TEXT DEFAULT '';")
        if "approver_name" not in columns:
            connection.execute("ALTER TABLE projects ADD COLUMN approver_name TEXT DEFAULT '';")
        if "program" not in columns:
            connection.execute("ALTER TABLE projects ADD COLUMN program TEXT DEFAULT '';")
            # Projetos existentes eram todos do fluxo Tekla detalhado.
            connection.execute(
                "UPDATE projects SET program = ? WHERE program IS NULL OR program = '';",
                (DEFAULT_PROGRAM,),
            )
        if "stage_mode" not in columns:
            connection.execute("ALTER TABLE projects ADD COLUMN stage_mode TEXT DEFAULT '';")
            connection.execute(
                "UPDATE projects SET stage_mode = ? WHERE stage_mode IS NULL OR stage_mode = '';",
                (DEFAULT_STAGE_MODE,),
            )

    def _ensure_project_stage_columns(self, connection: sqlite3.Connection) -> None:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(project_stages);").fetchall()}
        if "time_adjustment_seconds" not in columns:
            connection.execute(
                "ALTER TABLE project_stages ADD COLUMN time_adjustment_seconds INTEGER NOT NULL DEFAULT 0;"
            )

    def _ensure_revision_columns(self, connection: sqlite3.Connection) -> None:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(project_revision_history);").fetchall()}
        if "revision_date" not in columns:
            connection.execute("ALTER TABLE project_revision_history ADD COLUMN revision_date TEXT DEFAULT '';")
        if "added_seconds" not in columns:
            connection.execute("ALTER TABLE project_revision_history ADD COLUMN added_seconds INTEGER NOT NULL DEFAULT 0;")
        connection.execute(
            "UPDATE project_revision_history SET revision_date = COALESCE(NULLIF(revision_date, ''), created_at, completed_at, '') "
            "WHERE revision_date IS NULL OR revision_date = '';"
        )

    def _ensure_indicator_columns(self, connection: sqlite3.Connection) -> None:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(project_indicators);").fetchall()}
        if "page_count" not in columns:
            connection.execute(
                "ALTER TABLE project_indicators ADD COLUMN page_count INTEGER NOT NULL DEFAULT 0;"
            )

        # Recupera automaticamente a quantidade de folhas dos PDFs que já
        # tinham sido importados em revisões anteriores. Se o arquivo não
        # estiver mais no caminho original, o valor permanece 0 até a próxima
        # importação, sem impedir a abertura do programa.
        try:
            from pypdf import PdfReader
        except ImportError:
            return
        rows = connection.execute(
            "SELECT project_id, source_path FROM project_indicators "
            "WHERE page_count <= 0 AND source_type = 'pdf' AND source_path <> '';"
        ).fetchall()
        for row in rows:
            source = Path(str(row["source_path"] or ""))
            if not source.is_file() or source.suffix.lower() != ".pdf":
                continue
            try:
                page_count = len(PdfReader(str(source)).pages)
            except Exception:
                continue
            if page_count > 0:
                connection.execute(
                    "UPDATE project_indicators SET page_count = ? WHERE project_id = ?;",
                    (page_count, int(row["project_id"])),
                )

    def _backup_existing_database(self) -> None:
        if not self.db_path.exists() or self.db_path.stat().st_size == 0:
            return
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        backup_path = BACKUP_DIR / f"controle_projetos_{utcish_file_stamp()}.sqlite"
        try:
            shutil.copy2(self.db_path, backup_path)
        except OSError:
            return
        backups = sorted(BACKUP_DIR.glob("controle_projetos_*.sqlite"), key=lambda item: item.stat().st_mtime, reverse=True)
        for old in backups[5:]:
            try:
                old.unlink()
            except OSError:
                pass

    def _seed_defaults(self, connection: sqlite3.Connection) -> None:
        timestamp = now_iso()
        row = connection.execute("SELECT id FROM stage_weight_profiles WHERE is_default = 1 LIMIT 1;").fetchone()
        if row is None:
            connection.execute(
                """
                INSERT INTO stage_weight_profiles (name, is_default, smoothing_factor, weights_json, created_at, updated_at)
                VALUES (?, 1, 0.30, ?, ?, ?);
                """,
                ("Perfil padrão provisório", to_json(default_weights()), timestamp, timestamp),
            )
        defaults = {
            "user_name": "CaLavort",
            "user_nickname": "CaLavort",
            "projectist_name": "",
            "projectist_abbreviation": "JEC",
            "report_output_format": "html",
            "project_statuses": to_json(["Em andamento", "Planejamento", "Pausado", "Concluído"]),
            "project_form_suggestions": to_json({
                "os_number": [], "client": [], "area": [], "checker_name": [], "approver_name": []
            }),
            "auto_description_enabled": "1",
            "last_model_path": "",
            "last_model_parent": "",
            "last_document_folder": "",
            "last_document_parent": "",
            "last_spreadsheet_path": "",
            "last_spreadsheet_parent": "",
        }
        for key, value in defaults.items():
            connection.execute(
                """
                INSERT INTO settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO NOTHING;
                """,
                (key, value, timestamp),
            )

        # A revisão anterior usava 15 minutos de tolerância, o que podia
        # acrescentar tempo demais após o usuário parar de trabalhar. Migra
        # apenas o valor padrão antigo, preservando qualquer ajuste manual.
        connection.execute(
            """
            UPDATE settings
            SET value = '300', updated_at = ?
            WHERE key = 'inactivity_tolerance_seconds' AND value = '900';
            """,
            (timestamp,),
        )

    @staticmethod
    def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def setting(self, key: str, default: str = "") -> str:
        with self._lock, self.session() as connection:
            row = connection.execute("SELECT value FROM settings WHERE key = ?;", (key,)).fetchone()
            return str(row["value"]) if row else default

    def set_setting(self, key: str, value: Any, connection: sqlite3.Connection | None = None) -> None:
        owns_connection = connection is None
        conn = connection or self.connect()
        try:
            conn.execute(
                """
                INSERT INTO settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at;
                """,
                (key, str(value), now_iso()),
            )
            if owns_connection:
                conn.commit()
        finally:
            if owns_connection:
                conn.close()

    def get_weight_profile(self) -> dict[str, Any]:
        with self._lock, self.session() as connection:
            row = connection.execute(
                "SELECT * FROM stage_weight_profiles WHERE is_default = 1 AND archived_at IS NULL ORDER BY id DESC LIMIT 1;"
            ).fetchone()
            if row is None:
                return {"id": None, "name": "Perfil padrão provisório", "weights": default_weights(), "smoothing_factor": 0.30}
            data = dict(row)
            data["weights"] = from_json(data.pop("weights_json"), default_weights())
            return data

    def update_weight_profile(self, weights: dict[str, Any], name: str = "Perfil padrão provisório") -> dict[str, Any]:
        clean = normalize_weights({str(key): parse_float(value) for key, value in weights.items()})
        timestamp = now_iso()
        with self._lock, self.session() as connection:
            profile = connection.execute("SELECT id FROM stage_weight_profiles WHERE is_default = 1 LIMIT 1;").fetchone()
            if profile:
                connection.execute(
                    """
                    UPDATE stage_weight_profiles
                    SET name = ?, weights_json = ?, updated_at = ?
                    WHERE id = ?;
                    """,
                    (name, to_json(clean), timestamp, int(profile["id"])),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO stage_weight_profiles (name, is_default, smoothing_factor, weights_json, created_at, updated_at)
                    VALUES (?, 1, 0.30, ?, ?, ?);
                    """,
                    (name, to_json(clean), timestamp, timestamp),
                )
            for order, weight in clean.items():
                connection.execute(
                    """
                    UPDATE project_stages
                    SET weight = ?, updated_at = ?
                    WHERE stage_order = ? AND project_id IN (
                        SELECT id FROM projects WHERE status NOT IN ('Concluído', 'Arquivado')
                    );
                    """,
                    (float(weight), timestamp, int(order)),
                )
            self.add_activity(connection, None, "pesos", "atualizar", "Perfil de pesos atualizado.", {"weights": clean})
        return self.get_weight_profile()

    def create_project(self, data: dict[str, Any]) -> dict[str, Any]:
        timestamp = now_iso()
        code = str(data.get("code") or "").strip()
        title = str(data.get("title") or "").strip()
        if not code:
            raise ValueError("Código do projeto é obrigatório.")
        if not title:
            raise ValueError("Título do projeto é obrigatório.")

        payload = {field: str(data.get(field) or "").strip() for field in PROJECT_FIELDS}
        configured_projectist = self.setting("projectist_name", "").strip()
        # Configurações fornece o valor inicial, mas cada projeto pode manter
        # um responsável e um projetista próprios quando o usuário editar os campos.
        if configured_projectist and not payload.get("responsible"):
            payload["responsible"] = configured_projectist
        if configured_projectist and not payload.get("projectist_name"):
            payload["projectist_name"] = configured_projectist
        payload["code"] = code
        payload["title"] = title
        payload["status"] = payload.get("status") or "Em andamento"
        payload["priority"] = payload.get("priority") or "Normal"
        payload["revision"] = payload.get("revision") or "0"
        payload["program"] = payload.get("program") if payload.get("program") in PROGRAMS else DEFAULT_PROGRAM
        payload["stage_mode"] = payload.get("stage_mode") if payload.get("stage_mode") in STAGE_MODES else DEFAULT_STAGE_MODE
        self._validate_project_date_order(payload.get("start_date"), payload.get("due_date"))

        with self._lock, self.session() as connection:
            duplicate = connection.execute("SELECT id FROM projects WHERE code = ?;", (code,)).fetchone()
            if duplicate is not None:
                raise ValueError(f"Ja existe um projeto cadastrado com o codigo {code}. Use Trocar projeto ou edite o cadastro existente.")
            is_completed = payload.get("status") == "Concluído"
            # Todo projeto novo em andamento passa a ser o projeto ativo imediatamente,
            # iniciando sua contagem no instante do cadastro. Projetos cadastrados já
            # como concluídos permanecem fora do relógio corrente.
            make_active = not is_completed
            if make_active:
                self._heartbeat_runtime_session(connection, reference=timestamp)
                connection.execute("UPDATE projects SET active = 0;")
            columns = [
                "code",
                "os_number",
                "title",
                "client",
                "responsible",
                "projectist_name",
                "checker_name",
                "approver_name",
                "status",
                "priority",
                "revision",
                "program",
                "stage_mode",
                "discipline",
                "area",
                "description",
                "model_path",
                "model_name",
                "document_folder",
                "spreadsheet_path",
                "start_date",
                "due_date",
                "active",
                "created_at",
                "updated_at",
            ]
            values = [payload.get(column, "") for column in columns[:-3]] + [1 if make_active else 0, timestamp, timestamp]
            cursor = connection.execute(
                f"INSERT INTO projects ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)});",
                values,
            )
            project_id = int(cursor.lastrowid)
            self._seed_project_stages(
                connection,
                project_id,
                payload["program"],
                payload["stage_mode"],
                start_first_at=timestamp if make_active else None,
            )
            self._clear_deleted_project_memory(
                connection,
                code=code,
                model_path=payload.get("model_path", ""),
                model_name=payload.get("model_name", ""),
            )
            self._remember_project_form_suggestions(connection, payload)
            self._remember_project_locations(connection, payload)
            self.add_activity(connection, project_id, "projeto", "criar", f"Projeto {code} criado.", payload)
            if is_completed:
                # Todas as etapas concluídas + horas estimadas pela jornada (datas da legenda).
                completion_at = self._completion_timestamp(payload.get("due_date", ""), timestamp)
                connection.execute(
                    """
                    UPDATE project_stages
                    SET status = 'Concluída', internal_percent = 100, source = 'manual',
                        manual_confirmed = 1, completed_at = ?, updated_at = ?
                    WHERE project_id = ?;
                    """,
                    (completion_at, timestamp, project_id),
                )
                connection.execute("UPDATE projects SET completed_at = ? WHERE id = ?;", (completion_at, project_id))
                self._register_historical_period(
                    connection, project_id, payload.get("start_date", ""), payload.get("due_date", "")
                )
                revision_history = data.get("revision_history")
                if not revision_history and payload.get("due_date"):
                    revision_history = [{
                        "revision": payload.get("revision") or "0",
                        "revision_date": payload.get("due_date") or "",
                    }]
                self._sync_project_revision_history(connection, project_id, revision_history)
                self.add_activity(connection, project_id, "projeto", "concluir", f"Projeto {code} cadastrado como concluído.")
            else:
                # Se a data de início for anterior a hoje, estima o período já trabalhado
                # usando a jornada definida em app/worktime.py. O período corrente começa
                # no instante do cadastro/ativação, sem duplicar as horas históricas.
                self._backfill_start_date_period(
                    connection, project_id, payload.get("start_date", ""), timestamp
                )
                if make_active:
                    self._open_active_period(connection, project_id, timestamp)
                    self._start_runtime_session(connection, project_id, timestamp)
                    self.add_activity(connection, project_id, "projeto", "ativar", f"Projeto {code} definido como ativo.")
        return self.get_project(project_id) or {}

    @staticmethod
    def _completion_timestamp(date_value: Any, fallback_timestamp: str) -> str:
        """Converte a data de conclusão informada no cadastro em timestamp local."""
        text = str(date_value or "").strip()
        parsed: date | None = None
        try:
            parsed = date.fromisoformat(text[:10])
        except (TypeError, ValueError):
            match = re.match(r"^(\d{2})[/-](\d{2})[/-](\d{4})", text)
            if match:
                try:
                    parsed = date(int(match.group(3)), int(match.group(2)), int(match.group(1)))
                except ValueError:
                    parsed = None
        if parsed is None:
            return fallback_timestamp
        local_now = datetime.now().astimezone()
        return datetime(
            parsed.year,
            parsed.month,
            parsed.day,
            local_now.hour,
            local_now.minute,
            local_now.second,
            tzinfo=local_now.tzinfo,
        ).isoformat(timespec="seconds")

    @staticmethod
    def _date_from_iso_prefix(value: Any) -> date | None:
        try:
            return date.fromisoformat(str(value or "")[:10])
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _validate_project_date_order(start_date: Any, due_date: Any) -> None:
        start = ControleDatabase._date_from_iso_prefix(start_date)
        due = ControleDatabase._date_from_iso_prefix(due_date)
        if start and due and start > due:
            raise ValueError("A data de início não pode ser posterior à data de término.")

    @staticmethod
    def _swapped_month_day_date(value: Any) -> date | None:
        match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", str(value or "").strip())
        if not match:
            return None
        year = int(match.group(1))
        month = int(match.group(2))
        day = int(match.group(3))
        if not (1 <= day <= 12):
            return None
        try:
            return date(year, day, month)
        except ValueError:
            return None

    @staticmethod
    def _replace_timestamp_date(timestamp: Any, new_date: date, fallback_timestamp: str) -> str:
        text = str(timestamp or "").strip()
        match = re.match(r"^\d{4}-\d{2}-\d{2}(T.*)$", text)
        if match:
            return f"{new_date.isoformat()}{match.group(1)}"
        return ControleDatabase._completion_timestamp(new_date.isoformat(), fallback_timestamp)

    def _remember_project_form_suggestions(
        self, connection: sqlite3.Connection, values: dict[str, Any]
    ) -> None:
        row = connection.execute(
            "SELECT value FROM settings WHERE key = 'project_form_suggestions';"
        ).fetchone()
        saved = from_json(
            row["value"] if row else "",
            {"os_number": [], "client": [], "area": [], "checker_name": [], "approver_name": []},
        )
        if not isinstance(saved, dict):
            saved = {"os_number": [], "client": [], "area": [], "checker_name": [], "approver_name": []}
        changed = False
        for field in ("os_number", "client", "area", "checker_name", "approver_name"):
            existing = saved.get(field) if isinstance(saved.get(field), list) else []
            clean_existing: list[str] = []
            for item in existing:
                clean = str(item or "").strip()
                if clean and clean.casefold() not in {value.casefold() for value in clean_existing}:
                    clean_existing.append(clean)
            new_value = str(values.get(field) or "").strip()
            if new_value and new_value.casefold() not in {value.casefold() for value in clean_existing}:
                clean_existing.append(new_value)
                changed = True
            saved[field] = clean_existing
        if changed or row is None:
            self.set_setting("project_form_suggestions", to_json(saved), connection)

    @staticmethod
    def _parent_location(value: Any, *, file_path: bool = False) -> str:
        text = str(value or "").strip().rstrip("\\/")
        if not text:
            return ""
        # ntpath mantém o comportamento correto para caminhos do Windows,
        # mesmo durante testes em outro sistema operacional.
        parent = ntpath.dirname(text)
        if not parent and file_path:
            parent = ntpath.dirname(str(value or "").strip())
        return parent or text

    def _remember_project_locations(
        self, connection: sqlite3.Connection, values: dict[str, Any]
    ) -> None:
        model_path = str(values.get("model_path") or "").strip()
        document_folder = str(values.get("document_folder") or "").strip()
        spreadsheet_path = str(values.get("spreadsheet_path") or "").strip()
        if model_path:
            self.set_setting("last_model_path", model_path, connection)
            self.set_setting("last_model_parent", self._parent_location(model_path), connection)
        if document_folder:
            self.set_setting("last_document_folder", document_folder, connection)
            self.set_setting("last_document_parent", self._parent_location(document_folder), connection)
        if spreadsheet_path:
            self.set_setting("last_spreadsheet_path", spreadsheet_path, connection)
            self.set_setting(
                "last_spreadsheet_parent",
                self._parent_location(spreadsheet_path, file_path=True),
                connection,
            )

    @staticmethod
    def _detail_match_key(value: Any) -> str:
        return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())

    @classmethod
    def _is_detail_progress_stage(cls, name: Any) -> bool:
        normalized = re.sub(r"\s+", " ", str(name or "").strip().casefold())
        return normalized in DETAIL_STAGE_NAMES

    @staticmethod
    def _load_detail_progress_projects() -> dict[str, Any]:
        try:
            if not DETAIL_PROGRESS_HISTORY.is_file():
                return {}
            data = json.loads(DETAIL_PROGRESS_HISTORY.read_text(encoding="utf-8"))
            projects = data.get("projects", {}) if isinstance(data, dict) else {}
            return projects if isinstance(projects, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _detail_progress_timestamp(entry: dict[str, Any]) -> float:
        """Retorna a data mais recente do registro para desempatar duplicidades."""
        best = 0.0
        for field in ("last_analysis_at", "last_tick_at", "updated_at", "created_at"):
            text = str(entry.get(field) or "").strip()
            if not text:
                continue
            try:
                best = max(best, datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
            except Exception:
                continue
        return best

    @staticmethod
    def _detail_progress_sheet_total(entry: dict[str, Any]) -> int:
        """Maior folha que ja tem item detalhado no ultimo retrato do Tekla."""
        if "last_scan_sheet_total" in entry and entry.get("last_scan_sheet_total") is not None:
            return max(0, parse_int(entry.get("last_scan_sheet_total"), 0))

        highest = 0

        def bump(value: Any) -> None:
            nonlocal highest
            page = parse_int(value, 0)
            if page > highest:
                highest = page

        def consume(values: Any) -> None:
            if isinstance(values, dict):
                for field in (
                    "number",
                    "page",
                    "page_number",
                    "sheet",
                    "sheet_number",
                    "temporary_page",
                ):
                    if field in values:
                        bump(values.get(field))
                for field in (
                    "pages",
                    "scope_pages",
                    "selected_pages",
                    "detected_pages",
                    "temporary_pages",
                    "temporary_page_numbers",
                    "final_pages",
                    "definitive_pages",
                ):
                    if field in values:
                        consume(values.get(field))
                return
            if isinstance(values, (str, int, float)):
                values = [values]
            if not isinstance(values, (list, tuple, set)):
                return
            for value in values:
                if isinstance(value, dict):
                    consume(value)
                else:
                    bump(value)

        items = entry.get("items", {})
        if isinstance(items, dict):
            for item in items.values():
                if isinstance(item, dict):
                    consume(item.get("pages", []))

        # Compatibilidade com possíveis formatos de histórico anteriores ou
        # posteriores do Progresso de detalhamento.
        item_pages = entry.get("item_pages", {})
        if isinstance(item_pages, dict):
            for values in item_pages.values():
                consume(values)
        for item in entry.get("detailed_items", []) or []:
            if isinstance(item, dict):
                consume(item.get("pages", []))
        return highest

    @staticmethod
    def _latest_datetime_value(*values: Any) -> str:
        """Retorna, sem alterar o texto original, a data/hora mais recente."""
        best_value = ""
        best_timestamp = float("-inf")
        for value in values:
            text = str(value or "").strip()
            if not text:
                continue
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.astimezone()
                timestamp = parsed.timestamp()
            except Exception:
                continue
            if timestamp > best_timestamp:
                best_timestamp = timestamp
                best_value = text
        return best_value

    @staticmethod
    def _detail_progress_snapshot(entry: dict[str, Any]) -> dict[str, float] | None:
        detailed = parse_float(entry.get("last_scan_detailed_count"), -1)
        if detailed < 0:
            detailed_values = entry.get("last_scan_detailed", [])
            detailed = float(len(detailed_values)) if isinstance(detailed_values, list) else 0.0
        total = parse_float(entry.get("last_scan_total"), 0)
        missing = parse_float(entry.get("last_scan_missing_count"), -1)
        if total <= 0 and missing >= 0:
            total = detailed + missing
        if total <= 0:
            return None
        detailed = max(0.0, min(detailed, total))
        return {
            "progress": round(clamp((detailed * 100.0) / total), 1),
            "detailed": detailed,
            "total": max(0.0, total),
            "missing": max(0.0, total - detailed if missing < 0 else missing),
        }

    def _detail_progress_for_project(self, project: dict[str, Any]) -> dict[str, Any] | None:
        projects = self._load_detail_progress_projects()
        project_code = self._detail_match_key(project.get("code"))
        project_model_path = self._detail_match_key(project.get("model_path"))
        best_rank: tuple[int, float] | None = None
        best_entry: dict[str, Any] | None = None
        best_snapshot: dict[str, float] | None = None

        for entry in projects.values():
            if not isinstance(entry, dict):
                continue
            score = 0
            for field in ("project_number", "drawing_name"):
                if project_code and self._detail_match_key(entry.get(field)) == project_code:
                    score = max(score, 100)
            if project_model_path and self._detail_match_key(entry.get("model_path")) == project_model_path:
                score = max(score, 95)
            for field in ("model_name", "model_path", "display_name"):
                value = self._detail_match_key(entry.get(field))
                if project_code and project_code in value:
                    score = max(score, 80)
            if score <= 0:
                continue

            snapshot = self._detail_progress_snapshot(entry)
            if snapshot is None:
                continue
            rank = (score, self._detail_progress_timestamp(entry))
            if best_rank is None or rank > best_rank:
                best_rank = rank
                best_entry = entry
                best_snapshot = snapshot

        if best_entry is None or best_snapshot is None:
            return None

        # Pode haver mais de um registro para o mesmo número de projeto quando o
        # nome do modelo ou do desenho muda. O registro mais recente é a fonte
        # oficial; assim o Controle de Projeto não fica preso em uma leitura antiga.
        return {
            **best_snapshot,
            "project_number": best_entry.get("project_number") or best_entry.get("drawing_name") or "",
            "last_analysis_at": best_entry.get("last_analysis_at") or "",
            "last_update_at": self._latest_datetime_value(
                best_entry.get("last_analysis_at"),
                best_entry.get("last_tick_at"),
                best_entry.get("updated_at"),
                best_entry.get("created_at"),
            ),
            "sheet_total": self._detail_progress_sheet_total(best_entry),
        }

    def _sync_detail_progress_stage(
        self,
        connection: sqlite3.Connection,
        project_id: int,
        project: dict[str, Any] | None = None,
    ) -> bool:
        if project is None:
            row = connection.execute("SELECT * FROM projects WHERE id = ?;", (project_id,)).fetchone()
            project = dict(row) if row else {}
        if not project:
            return False
        program = str(project.get("program") or DEFAULT_PROGRAM).strip().casefold()
        if program and not program.startswith("tekla"):
            return False
        detail = self._detail_progress_for_project(project)
        if not detail:
            return False
        stages = connection.execute(
            "SELECT * FROM project_stages WHERE project_id = ? ORDER BY stage_order ASC;",
            (project_id,),
        ).fetchall()
        stage = next((dict(row) for row in stages if self._is_detail_progress_stage(row["name"])), None)
        if not stage:
            return False
        if (
            str(stage.get("source") or "") == REOPEN_STAGE_SOURCE
            and str(stage.get("status") or "") != "Concluída"
            and float(stage.get("internal_percent") or 0) < 100
        ):
            return False
        if int(stage.get("manual_confirmed") or 0) and float(stage.get("internal_percent") or 0) >= 100:
            return False

        percent = float(detail["progress"])
        current_percent = float(stage.get("internal_percent") or 0)
        current_status = str(stage.get("status") or "")
        status = "Concluída" if percent >= 100 else ("Em andamento" if percent > 0 or current_status == "Em andamento" else "Não iniciada")
        expected_total = float(detail["total"])
        observed_done = float(detail["detailed"])
        changed = (
            abs(current_percent - percent) >= 0.05
            or current_status != status
            or str(stage.get("source") or "") != DETAIL_PROGRESS_SOURCE
            or abs(float(stage.get("expected_total") or 0) - expected_total) >= 0.05
            or abs(float(stage.get("observed_done") or 0) - observed_done) >= 0.05
        )
        detail_update_text = str(detail.get("last_update_at") or "").strip()
        detail_update_at = self._parse_local_datetime(detail_update_text)
        stage_updated_at = self._parse_local_datetime(stage.get("updated_at"))
        timeline_changed = bool(
            detail_update_at
            and (stage_updated_at is None or detail_update_at > stage_updated_at)
        )
        if not changed and not timeline_changed:
            return False

        timestamp = detail_update_text if detail_update_at else now_iso()
        if not changed:
            connection.execute(
                """
                UPDATE project_stages
                SET last_activity_at = ?, updated_at = ?
                WHERE id = ?;
                """,
                (timestamp, timestamp, int(stage["id"])),
            )
            self._sync_stage_timer(connection, project_id, timestamp)
            return True

        started_at = stage.get("started_at") or (timestamp if percent > 0 or status == "Em andamento" else None)
        completed_at = stage.get("completed_at") if status == "Concluída" and stage.get("completed_at") else (timestamp if status == "Concluída" else None)
        connection.execute(
            """
            UPDATE project_stages
            SET status = ?, internal_percent = ?, source = ?, confidence = ?,
                started_at = ?, last_activity_at = ?, completed_at = ?,
                expected_total = ?, observed_done = ?, manual_confirmed = 0, updated_at = ?
            WHERE id = ?;
            """,
            (
                status,
                percent,
                DETAIL_PROGRESS_SOURCE,
                1.0,
                started_at,
                timestamp,
                completed_at,
                expected_total,
                observed_done,
                timestamp,
                int(stage["id"]),
            ),
        )
        message = f"Detalhamento automatico: {observed_done:.0f} de {expected_total:.0f} ({percent:.1f}%)."
        self.add_stage_observation(
            connection,
            int(stage["id"]),
            DETAIL_PROGRESS_SOURCE,
            1.0,
            message,
            detail,
        )
        self.add_activity(connection, project_id, "etapa", "sincronizar", message)
        if status == "Concluída":
            self._start_next_stage(connection, project_id, int(stage["stage_order"]), timestamp)
        self._sync_stage_timer(connection, project_id, timestamp)
        return True

    def _normalize_revision_history(self, value: Any) -> list[dict[str, str]]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("As datas das revisões devem ser enviadas em uma lista.")
        result: list[dict[str, str]] = []
        seen: set[str] = set()
        for raw in value:
            if not isinstance(raw, dict):
                continue
            revision = str(raw.get("revision") or "").strip()
            revision_date = str(raw.get("revision_date") or "").strip()
            if not revision and not revision_date:
                continue
            if not revision or not revision_date:
                raise ValueError("Informe a revisão e a respectiva data.")
            try:
                parsed_date = date.fromisoformat(revision_date[:10])
            except ValueError as exc:
                raise ValueError(f"Data inválida para a revisão {revision}.") from exc
            key = revision.casefold()
            if key in seen:
                raise ValueError(f"A revisão {revision} foi informada mais de uma vez.")
            seen.add(key)
            result.append({"revision": revision, "revision_date": parsed_date.isoformat()})
        return result

    def _sync_project_revision_history(
        self,
        connection: sqlite3.Connection,
        project_id: int,
        revision_history: Any,
    ) -> None:
        entries = self._normalize_revision_history(revision_history)
        if not entries:
            return
        timestamp = now_iso()
        for item in entries:
            revision = item["revision"]
            revision_date = self._completion_timestamp(item["revision_date"], timestamp)
            connection.execute(
                """
                INSERT INTO project_revision_history
                (project_id, revision, snapshot_json, completed_at, created_at, revision_date, added_seconds)
                VALUES (?, ?, ?, NULL, ?, ?, 0)
                ON CONFLICT(project_id, revision) DO UPDATE SET
                    revision_date = excluded.revision_date,
                    snapshot_json = CASE
                        WHEN project_revision_history.snapshot_json IS NULL
                             OR project_revision_history.snapshot_json = ''
                             OR project_revision_history.snapshot_json = '{}'
                        THEN excluded.snapshot_json
                        ELSE project_revision_history.snapshot_json
                    END;
                """,
                (
                    project_id,
                    revision,
                    to_json({"registered_from_completed_project_form": True}),
                    timestamp,
                    revision_date,
                ),
            )

    def _repair_completed_project_metadata(self, connection: sqlite3.Connection) -> None:
        """Corrige uma única vez cadastros concluídos antigos, sem apagar registros."""
        marker = connection.execute(
            "SELECT value FROM settings WHERE key = 'completion_date_repair_v13';"
        ).fetchone()
        if marker and str(marker["value"] or "") == "1":
            return
        timestamp = now_iso()
        rows = connection.execute(
            "SELECT id, start_date, due_date, completed_at, active FROM projects WHERE status = 'Concluído';"
        ).fetchall()
        for row in rows:
            project_id = int(row["id"])
            completion_at = self._completion_timestamp(
                row["due_date"], str(row["completed_at"] or timestamp)
            )
            needs_period_repair = not row["completed_at"]
            connection.execute(
                "UPDATE projects SET completed_at = ? WHERE id = ?;",
                (completion_at, project_id),
            )
            connection.execute(
                """
                UPDATE project_stages
                SET status = 'Concluída', internal_percent = 100, source = 'manual',
                    manual_confirmed = 1, completed_at = COALESCE(completed_at, ?), updated_at = ?
                WHERE project_id = ? AND NOT (status = 'Concluída' OR internal_percent >= 100);
                """,
                (completion_at, timestamp, project_id),
            )
            if needs_period_repair:
                connection.execute("DELETE FROM project_active_periods WHERE project_id = ?;", (project_id,))
                self._register_historical_period(
                    connection, project_id, str(row["start_date"] or ""), str(row["due_date"] or "")
                )
        if rows:
            connection.execute(
                "UPDATE project_active_periods SET end_at = ? WHERE end_at IS NULL AND project_id IN "
                "(SELECT id FROM projects WHERE status = 'Concluído');",
                (timestamp,),
            )
        self.set_setting("completion_date_repair_v13", "1", connection)

    def _repair_inverted_completed_project_dates(self, connection: sqlite3.Connection) -> None:
        """Repair completed projects whose due date was saved before the start date."""
        timestamp = now_iso()
        rows = connection.execute(
            """
            SELECT id, code, start_date, due_date, completed_at
            FROM projects
            WHERE status LIKE 'Conclu%'
              AND start_date <> ''
              AND due_date <> '';
            """
        ).fetchall()
        for row in rows:
            start = self._date_from_iso_prefix(row["start_date"])
            due = self._date_from_iso_prefix(row["due_date"])
            if not start or not due or due >= start:
                continue
            corrected_due = self._swapped_month_day_date(row["due_date"])
            if not corrected_due or corrected_due < start:
                continue
            corrected_iso = corrected_due.isoformat()
            completed_at = self._replace_timestamp_date(row["completed_at"], corrected_due, timestamp)
            project_id = int(row["id"])
            old_due = str(row["due_date"] or "")
            connection.execute(
                "UPDATE projects SET due_date = ?, completed_at = ? WHERE id = ?;",
                (corrected_iso, completed_at, project_id),
            )
            connection.execute(
                """
                UPDATE project_stages
                SET completed_at = ?, updated_at = ?
                WHERE project_id = ? AND (status = 'ConcluÃ­da' OR internal_percent >= 100);
                """,
                (completed_at, timestamp, project_id),
            )
            connection.execute("DELETE FROM project_active_periods WHERE project_id = ?;", (project_id,))
            self._register_historical_period(connection, project_id, str(row["start_date"] or ""), corrected_iso)
            self.add_activity(
                connection,
                project_id,
                "projeto",
                "corrigir",
                f"Data de entrega corrigida de {old_due} para {corrected_iso}; horas recalculadas pela jornada.",
            )

    def _repair_project_43297_start_date(self, connection: sqlite3.Connection) -> None:
        marker = connection.execute(
            "SELECT value FROM settings WHERE key = 'project_43297_start_repair_v1';"
        ).fetchone()
        if marker and str(marker["value"] or "") == "1":
            return
        row = connection.execute(
            """
            SELECT id, start_date, due_date, status
            FROM projects
            WHERE code = 'IME-MC-1-43297'
            LIMIT 1;
            """
        ).fetchone()
        if row is None:
            return
        old_start = str(row["start_date"] or "")
        corrected_start = "2026-04-24"
        if old_start == corrected_start:
            self.set_setting("project_43297_start_repair_v1", "1", connection)
            return
        if old_start != "2026-09-24":
            return

        project_id = int(row["id"])
        due_date = str(row["due_date"] or "")
        timestamp = now_iso()
        connection.execute(
            "UPDATE projects SET start_date = ?, updated_at = ? WHERE id = ?;",
            (corrected_start, timestamp, project_id),
        )
        if str(row["status"] or "").startswith("Conclu"):
            connection.execute("DELETE FROM project_active_periods WHERE project_id = ?;", (project_id,))
            self._register_historical_period(connection, project_id, corrected_start, due_date)
        self.add_activity(
            connection,
            project_id,
            "projeto",
            "corrigir",
            f"Data de início corrigida de {old_start} para {corrected_start}; horas recalculadas pela jornada.",
        )
        self.set_setting("project_43297_start_repair_v1", "1", connection)

    def _seed_project_form_suggestions_from_projects(self, connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            "SELECT os_number, client, area, checker_name, approver_name FROM projects;"
        ).fetchall()
        for row in rows:
            self._remember_project_form_suggestions(connection, dict(row))

    def _seed_last_project_locations(self, connection: sqlite3.Connection) -> None:
        mappings = (
            ("model_path", "last_model_path", "last_model_parent", False),
            ("document_folder", "last_document_folder", "last_document_parent", False),
            ("spreadsheet_path", "last_spreadsheet_path", "last_spreadsheet_parent", True),
        )
        for field, path_key, parent_key, is_file in mappings:
            saved = connection.execute(
                "SELECT value FROM settings WHERE key = ?;", (path_key,)
            ).fetchone()
            if saved and str(saved["value"] or "").strip():
                continue
            row = connection.execute(
                f"SELECT {field} AS value FROM projects "
                f"WHERE TRIM(COALESCE({field}, '')) <> '' "
                "ORDER BY updated_at DESC, id DESC LIMIT 1;"
            ).fetchone()
            value = str(row["value"] or "").strip() if row else ""
            if not value:
                continue
            self.set_setting(path_key, value, connection)
            self.set_setting(parent_key, self._parent_location(value, file_path=is_file), connection)

    def _seed_project_stages(
        self,
        connection: sqlite3.Connection,
        project_id: int,
        program: str = DEFAULT_PROGRAM,
        mode: str = DEFAULT_STAGE_MODE,
        start_first_at: str | None = None,
    ) -> None:
        stages = self._template_stages(connection, program, mode)
        timestamp = now_iso()
        for item in stages:
            order = int(item["order"])
            if start_first_at and order == 1:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO project_stages
                    (project_id, stage_order, name, status, started_at, last_activity_at, weight, created_at, updated_at)
                    VALUES (?, ?, ?, 'Em andamento', ?, ?, ?, ?, ?);
                    """,
                    (
                        project_id,
                        order,
                        item["name"],
                        start_first_at,
                        start_first_at,
                        float(item["weight"]),
                        timestamp,
                        timestamp,
                    ),
                )
            else:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO project_stages
                    (project_id, stage_order, name, weight, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?);
                    """,
                    (project_id, order, item["name"], float(item["weight"]), timestamp, timestamp),
                )

    def _seed_stage_templates(self, connection: sqlite3.Connection) -> None:
        """Semeia os templates padrão por (programa × nível). Idempotente."""
        timestamp = now_iso()
        for program in PROGRAMS:
            for mode in STAGE_MODES:
                existing = connection.execute(
                    "SELECT 1 FROM stage_templates WHERE program = ? AND mode = ? LIMIT 1;",
                    (program, mode),
                ).fetchone()
                if existing:
                    continue
                for item in default_template_stages(program, mode):
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO stage_templates
                        (program, mode, stage_order, name, weight, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?);
                        """,
                        (program, mode, int(item["order"]), item["name"], float(item["weight"]), timestamp, timestamp),
                    )

    def _template_stages(self, connection: sqlite3.Connection, program: str, mode: str) -> list[dict[str, Any]]:
        """Etapas do template salvo para (programa × nível); cai no padrão de código se vazio."""
        rows = connection.execute(
            "SELECT stage_order, name, weight FROM stage_templates WHERE program = ? AND mode = ? ORDER BY stage_order ASC;",
            (program, mode),
        ).fetchall()
        if rows:
            return [{"order": int(r["stage_order"]), "name": str(r["name"]), "weight": float(r["weight"])} for r in rows]
        return default_template_stages(program, mode)

    def list_projects(self, search: str = "", status: str = "", include_archived: bool = True) -> list[dict[str, Any]]:
        query = "SELECT * FROM projects"
        clauses = []
        params: list[Any] = []
        if search:
            like = f"%{search.lower()}%"
            clauses.append("(lower(code) LIKE ? OR lower(os_number) LIKE ? OR lower(title) LIKE ? OR lower(client) LIKE ? OR lower(responsible) LIKE ?)")
            params.extend([like, like, like, like, like])
        if status:
            clauses.append("status = ?")
            params.append(status)
        if not include_archived:
            clauses.append("status <> 'Arquivado'")
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY active DESC, updated_at DESC, code ASC;"
        with self._lock, self.session() as connection:
            rows = [dict(row) for row in connection.execute(query, params).fetchall()]
            for row in rows:
                project_id = int(row["id"])
                self._sync_detail_progress_stage(connection, project_id, row)
                stages = self.get_project_stages(project_id, connection)
                row["progress"] = calculate_project_progress(stages)
                row["current_stage"] = (current_stage(stages) or {}).get("name", "-")
                detail_progress = self._detail_progress_for_project(row)
                row["sheet_total"] = self.project_sheet_total(project_id, connection, row, detail_progress)
                row["sheet_count"] = row["sheet_total"]
                latest_activity_row = connection.execute(
                    "SELECT created_at FROM activity_history WHERE project_id = ? ORDER BY id DESC LIMIT 1;",
                    (project_id,),
                ).fetchone()
                latest_activity_at = latest_activity_row["created_at"] if latest_activity_row else ""
                row["detail_progress_updated_at"] = (detail_progress or {}).get("last_update_at") or ""
                row["last_update_at"] = self._latest_datetime_value(
                    row.get("updated_at"), latest_activity_at, row["detail_progress_updated_at"]
                )
                worked_seconds = self._project_active_seconds(connection, project_id)
                row["active_seconds"] = worked_seconds
                row["hours"] = round(worked_seconds / 3600.0, 4)
                row["revisions"] = self.list_project_revisions(project_id, connection)
                row["issue_count"] = int(
                    connection.execute(
                        "SELECT COUNT(*) AS count FROM issues WHERE project_id = ? AND status <> 'Resolvida';",
                        (project_id,),
                    ).fetchone()["count"]
                )
                indicator_row = connection.execute(
                    "SELECT total_weight, page_count, source_type, source_name, imported_at, updated_at "
                    "FROM project_indicators WHERE project_id = ?;",
                    (project_id,),
                ).fetchone()
                assembly_count = int(
                    connection.execute(
                        "SELECT COUNT(*) AS count FROM project_indicator_assemblies WHERE project_id = ?;",
                        (project_id,),
                    ).fetchone()["count"]
                )
                row["indicator_total_weight"] = round(
                    float(indicator_row["total_weight"] or 0), 3
                ) if indicator_row else 0.0
                row["indicator_assembly_count"] = assembly_count
                row["indicator_page_count"] = int(indicator_row["page_count"] or 0) if indicator_row else 0
                row["indicator_source_type"] = str(indicator_row["source_type"] or "") if indicator_row else ""
                row["indicator_source_name"] = str(indicator_row["source_name"] or "") if indicator_row else ""
                row["indicator_imported_at"] = indicator_row["imported_at"] if indicator_row else None
                row["indicator_updated_at"] = indicator_row["updated_at"] if indicator_row else None
                row["indicators_registered"] = bool(indicator_row or assembly_count)
            return rows

    def get_project(self, project_id: int) -> dict[str, Any] | None:
        with self._lock, self.session() as connection:
            row = connection.execute("SELECT * FROM projects WHERE id = ?;", (project_id,)).fetchone()
            if row is None:
                return None
            project = dict(row)
            self._sync_detail_progress_stage(connection, project_id, project)
            stages = self.get_project_stages(project_id, connection)
            project["stages"] = stages
            project["progress"] = calculate_project_progress(stages)
            project["current_stage"] = current_stage(stages)
            project["issues"] = self.list_issues(project_id, connection)
            project["files"] = self.list_files(project_id, connection)
            project["drawings"] = self.list_drawings(project_id, connection)
            detail_progress = self._detail_progress_for_project(project)
            project["sheet_total"] = self.project_sheet_total(project_id, connection, project, detail_progress)
            project["sheet_count"] = project["sheet_total"]
            project["history"] = self.activity(project_id, 80, connection)
            latest_activity_at = project["history"][0]["created_at"] if project["history"] else ""
            project["detail_progress_updated_at"] = (detail_progress or {}).get("last_update_at") or ""
            project["last_update_at"] = self._latest_datetime_value(
                project.get("updated_at"), latest_activity_at, project["detail_progress_updated_at"]
            )
            project["time_records"] = self.list_time_records(project_id, connection)
            project["revisions"] = self.list_project_revisions(project_id, connection)
            project["indicators"] = self.get_project_indicators(project_id, connection)
            # Tempo de projeto pela jornada de trabalho (ver app/worktime.py).
            worked_seconds = self._project_active_seconds(connection, project_id)
            project["active_seconds"] = worked_seconds
            project["hours"] = round(worked_seconds / 3600.0, 4)
            return project

    def active_project(self, connection: sqlite3.Connection | None = None) -> dict[str, Any] | None:
        owns_connection = connection is None
        conn = connection or self.connect()
        try:
            row = conn.execute("SELECT id FROM projects WHERE active = 1 ORDER BY id DESC LIMIT 1;").fetchone()
            if row is None:
                return None
            return self.get_project(int(row["id"]))
        finally:
            if owns_connection:
                conn.close()

    def update_project(self, project_id: int, data: dict[str, Any]) -> dict[str, Any]:
        regenerate = bool(data.get("regenerate_stages"))
        revision_history_present = "revision_history" in data
        updates = {field: str(data[field]).strip() for field in PROJECT_FIELDS if field in data}
        if not updates and not regenerate and not revision_history_present:
            return self.get_project(project_id) or {}
        with self._lock, self.session() as connection:
            original_row = connection.execute("SELECT * FROM projects WHERE id = ?;", (project_id,)).fetchone()
            if original_row is None:
                raise ValueError("Projeto não encontrado.")
            original = dict(original_row)
            merged_for_validation = {**original, **updates}
            self._validate_project_date_order(
                merged_for_validation.get("start_date"),
                merged_for_validation.get("due_date"),
            )
            if "code" in updates:
                duplicate = connection.execute(
                    "SELECT id FROM projects WHERE code = ? AND id <> ?;",
                    (updates["code"], project_id),
                ).fetchone()
                if duplicate is not None:
                    raise ValueError(f"Ja existe outro projeto cadastrado com o codigo {updates['code']}.")
            if updates:
                timestamp = now_iso()
                updates["updated_at"] = timestamp
                assignments = ", ".join(f"{field} = ?" for field in updates)
                connection.execute(
                    f"UPDATE projects SET {assignments} WHERE id = ?;",
                    [*updates.values(), project_id],
                )
                self._clear_deleted_project_memory(
                    connection,
                    code=str(updates.get("code") or ""),
                    model_path=str(updates.get("model_path") or ""),
                    model_name=str(updates.get("model_name") or ""),
                )
                merged_values = {**original, **updates}
                self._remember_project_form_suggestions(connection, merged_values)
                self._remember_project_locations(connection, merged_values)
                self.add_activity(connection, project_id, "projeto", "editar", "Cadastro do projeto atualizado.", updates)

                new_status = str(updates.get("status", original.get("status") or "")).strip()
                status_changed = new_status != str(original.get("status") or "").strip()
                if new_status == "Concluído":
                    start_date = str(updates.get("start_date", original.get("start_date") or ""))
                    due_date = str(updates.get("due_date", original.get("due_date") or ""))
                    completion_at = self._completion_timestamp(due_date, timestamp)
                    self._heartbeat_runtime_session(connection, reference=timestamp)
                    connection.execute(
                        "UPDATE projects SET completed_at = ?, updated_at = ? WHERE id = ?;",
                        (completion_at, timestamp, project_id),
                    )
                    connection.execute(
                        """
                        UPDATE project_stages
                        SET status = 'Concluída', internal_percent = 100, source = 'manual',
                            manual_confirmed = 1, completed_at = COALESCE(completed_at, ?), updated_at = ?
                        WHERE project_id = ?;
                        """,
                        (completion_at, timestamp, project_id),
                    )
                    if status_changed or "start_date" in updates or "due_date" in updates or not original.get("completed_at"):
                        connection.execute("DELETE FROM project_active_periods WHERE project_id = ?;", (project_id,))
                        self._register_historical_period(connection, project_id, start_date, due_date)
                    self._stop_runtime_session(connection, timestamp)
                    self.add_activity(
                        connection, project_id, "projeto", "concluir",
                        "Projeto atualizado automaticamente para concluído.",
                    )
                elif "start_date" in updates:
                    self._backfill_start_date_period(
                        connection, project_id, updates.get("start_date", ""), timestamp
                    )
            if revision_history_present:
                current_status = str(updates.get("status", original.get("status") or "")).strip()
                if current_status == "Concluído":
                    self._sync_project_revision_history(
                        connection, project_id, data.get("revision_history")
                    )
            if regenerate:
                prow = connection.execute(
                    "SELECT program, stage_mode, active, status FROM projects WHERE id = ?;", (project_id,)
                ).fetchone()
                program = prow["program"] if prow and prow["program"] in PROGRAMS else DEFAULT_PROGRAM
                mode = prow["stage_mode"] if prow and prow["stage_mode"] in STAGE_MODES else DEFAULT_STAGE_MODE
                start_first_at = (
                    timestamp
                    if prow
                    and int(prow["active"] or 0)
                    and not str(prow["status"] or "").startswith("Conclu")
                    else None
                )
                connection.execute("DELETE FROM project_stages WHERE project_id = ?;", (project_id,))
                self._seed_project_stages(connection, project_id, program, mode, start_first_at=start_first_at)
                self.add_activity(
                    connection, project_id, "etapa", "regenerar",
                    f"Etapas regeneradas a partir do modelo {program} · {mode}.",
                )
        return self.get_project(project_id) or {}

    def update_all_projectists(self, name: str) -> int:
        """Sincroniza o responsável/projetista de todos os projetos com Configurações."""
        clean_name = str(name or "").strip()
        if not clean_name:
            return 0
        with self._lock, self.session() as connection:
            cursor = connection.execute(
                "UPDATE projects SET responsible = ?, projectist_name = ? "
                "WHERE COALESCE(responsible, '') <> ? OR COALESCE(projectist_name, '') <> ?;",
                (clean_name, clean_name, clean_name, clean_name),
            )
            return max(0, int(cursor.rowcount or 0))

    def update_active_projectist(self, name: str) -> dict[str, Any] | None:
        # Mantido para compatibilidade com chamadas antigas. A configuração do
        # projetista agora é global e deve refletir em todos os projetos.
        self.update_all_projectists(name)
        return self.active_project()

    def raise_project_revision(
        self,
        project_id: int,
        new_revision: str,
        added_seconds: int = 0,
        revision_date: str = "",
    ) -> dict[str, Any]:
        """Atualiza somente a revisão e acrescenta o tempo informado.

        A operação não reinicia etapas, não reabre o projeto e não apaga indicadores.
        Cada revisão fica registrada com data e tempo adicional.
        """
        clean_revision = str(new_revision or "").strip()
        if not clean_revision:
            raise ValueError("Informe a nova revisão.")
        seconds = max(0, int(added_seconds or 0))
        timestamp = now_iso()
        clean_date = str(revision_date or "").strip()
        if clean_date:
            try:
                revision_moment = datetime.fromisoformat(clean_date[:10]).replace(
                    hour=datetime.now().hour, minute=datetime.now().minute, second=datetime.now().second
                ).astimezone().isoformat(timespec="seconds")
            except ValueError:
                raise ValueError("Data da revisão inválida.")
        else:
            revision_moment = timestamp

        with self._lock, self.session() as connection:
            row = connection.execute("SELECT * FROM projects WHERE id = ?;", (project_id,)).fetchone()
            if row is None:
                raise ValueError("Projeto não encontrado.")
            project = dict(row)
            current_revision = str(project.get("revision") or "0").strip() or "0"
            if clean_revision == current_revision:
                raise ValueError("A nova revisão deve ser diferente da revisão atual.")
            completion_at = self._stage_completion_timestamp(connection, project_id)
            if str(project.get("status") or "") != "Concluído" and not completion_at:
                raise ValueError("Somente projetos concluídos podem receber uma nova revisão por esta função.")

            self._heartbeat_runtime_session(connection, reference=timestamp)
            snapshot = {
                "revision_from": current_revision,
                "revision_to": clean_revision,
                "added_seconds": seconds,
                "project_status": project.get("status"),
                "progress": calculate_project_progress(self.get_project_stages(project_id, connection)),
            }

            # Registra a revisão atual na primeira utilização, preservando sua data conhecida.
            current_date = project.get("completed_at") or completion_at or project.get("updated_at") or project.get("created_at") or timestamp
            connection.execute(
                """
                INSERT INTO project_revision_history
                (project_id, revision, snapshot_json, completed_at, created_at, revision_date, added_seconds)
                VALUES (?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(project_id, revision) DO NOTHING;
                """,
                (project_id, current_revision, to_json({"baseline": True}), project.get("completed_at") or completion_at, timestamp, current_date),
            )

            connection.execute(
                "UPDATE projects SET revision = ?, updated_at = ? WHERE id = ?;",
                (clean_revision, timestamp, project_id),
            )
            if seconds > 0:
                connection.execute(
                    """
                    INSERT INTO work_sessions
                    (project_id, active_seconds, source, note, created_at)
                    VALUES (?, ?, 'revision_adjustment', ?, ?);
                    """,
                    (project_id, seconds, f"Tempo acrescentado na Rev.{clean_revision}.", timestamp),
                )
            connection.execute(
                """
                INSERT INTO project_revision_history
                (project_id, revision, snapshot_json, completed_at, created_at, revision_date, added_seconds)
                VALUES (?, ?, ?, NULL, ?, ?, ?)
                ON CONFLICT(project_id, revision) DO UPDATE SET
                    snapshot_json = excluded.snapshot_json,
                    created_at = excluded.created_at,
                    revision_date = excluded.revision_date,
                    added_seconds = excluded.added_seconds;
                """,
                (project_id, clean_revision, to_json(snapshot), timestamp, revision_moment, seconds),
            )
            time_text = f"; {round(seconds / 3600.0, 2)} h adicionadas" if seconds else ""
            self.add_activity(
                connection, project_id, "revisão", "subir",
                f"Revisão alterada de Rev.{current_revision} para Rev.{clean_revision}{time_text}.",
                {
                    "revision_from": current_revision,
                    "revision_to": clean_revision,
                    "revision_date": revision_moment,
                    "added_seconds": seconds,
                },
            )
        return self.get_project(project_id) or {}

    def list_project_revisions(
        self, project_id: int, connection: sqlite3.Connection | None = None
    ) -> list[dict[str, Any]]:
        owns_connection = connection is None
        conn = connection or self.connect()
        try:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT revision, revision_date, added_seconds, created_at "
                    "FROM project_revision_history WHERE project_id = ? "
                    "ORDER BY COALESCE(NULLIF(revision_date, ''), created_at) DESC, id DESC;",
                    (project_id,),
                ).fetchall()
            ]
        finally:
            if owns_connection:
                conn.close()

    def delete_project(self, project_id: int) -> None:
        with self._lock, self.session() as connection:
            row = connection.execute("SELECT code, model_path, model_name FROM projects WHERE id = ?;", (project_id,)).fetchone()
            if row is not None:
                self._remember_deleted_project(connection, row)
            connection.execute("DELETE FROM activity_history WHERE project_id = ?;", (project_id,))
            connection.execute("DELETE FROM projects WHERE id = ?;", (project_id,))
            code = row["code"] if row else str(project_id)
            self.add_activity(connection, None, "projeto", "excluir", f"Projeto {code} excluído.")

    # Tabelas de dados operacionais (limpas no reset). settings, stage_templates e
    # stage_weight_profiles são PRESERVADOS.
    OPERATIONAL_TABLES = (
        "activity_history",
        "app_presence",
        "deleted_project_memory",
        "drawings",
        "issues",
        "project_active_periods",
        "project_files",
        "project_indicator_assemblies",
        "project_indicators",
        "project_revision_history",
        "project_stages",
        "projects",
        "stage_observations",
        "stage_time_records",
        "work_sessions",
    )

    def _runtime_reference(self, reference: str | datetime | None = None) -> datetime:
        if isinstance(reference, datetime):
            return reference.replace(tzinfo=None)
        parsed = self._parse_local_datetime(reference) if reference else None
        return parsed or datetime.now()

    def _runtime_target_project(self, connection: sqlite3.Connection) -> int | None:
        row = connection.execute(
            "SELECT id FROM projects WHERE active = 1 ORDER BY id DESC LIMIT 1;"
        ).fetchone()
        if row is None:
            return None
        project_id = int(row["id"] or 0)
        if project_id <= 0 or self._stage_completion_timestamp(connection, project_id):
            return None
        return project_id

    def _start_runtime_session(
        self, connection: sqlite3.Connection, project_id: int, reference: str | datetime | None = None
    ) -> None:
        moment = self._runtime_reference(reference)
        timestamp = moment.isoformat(timespec="seconds")
        cursor = connection.execute(
            """
            INSERT INTO work_sessions
            (project_id, started_at, ended_at, active_seconds, source, note, created_at)
            VALUES (?, ?, ?, 0, 'app_open_extra', ?, ?);
            """,
            (project_id, timestamp, timestamp, "Tempo adicional com o programa aberto fora do expediente.", timestamp),
        )
        self._runtime_project_id = int(project_id)
        self._runtime_session_id = int(cursor.lastrowid)
        self._runtime_last_heartbeat = moment

    def _flush_runtime_interval(
        self, connection: sqlite3.Connection, reference: str | datetime | None = None
    ) -> None:
        if not self._runtime_project_id or not self._runtime_session_id or not self._runtime_last_heartbeat:
            return
        moment = self._runtime_reference(reference)
        if moment <= self._runtime_last_heartbeat:
            return
        extra = outside_schedule_seconds(
            self._runtime_last_heartbeat, moment, self._presence_days(connection)
        )
        connection.execute(
            "UPDATE work_sessions SET active_seconds = active_seconds + ?, ended_at = ? WHERE id = ?;",
            (extra, moment.isoformat(timespec="seconds"), self._runtime_session_id),
        )
        self._runtime_last_heartbeat = moment

    def _stop_runtime_session(
        self, connection: sqlite3.Connection, reference: str | datetime | None = None
    ) -> None:
        if self._runtime_session_id:
            self._flush_runtime_interval(connection, reference)
            moment = self._runtime_reference(reference)
            connection.execute(
                "UPDATE work_sessions SET ended_at = ? WHERE id = ?;",
                (moment.isoformat(timespec="seconds"), self._runtime_session_id),
            )
        self._runtime_project_id = None
        self._runtime_session_id = None
        self._runtime_last_heartbeat = None

    def _heartbeat_runtime_session(
        self, connection: sqlite3.Connection, reference: str | datetime | None = None
    ) -> None:
        """Registra o tempo fora da jornada enquanto o programa permanece aberto.

        A jornada normal continua sendo calculada pelos períodos ativos. Aqui é
        persistido apenas o complemento fora do expediente, evitando contagem dupla.
        """
        moment = self._runtime_reference(reference)
        target = self._runtime_target_project(connection)
        if self._runtime_project_id and self._runtime_project_id != target:
            self._stop_runtime_session(connection, moment)
        if target is None:
            return
        if self._runtime_project_id != target or not self._runtime_session_id:
            self._start_runtime_session(connection, target, moment)
            return
        self._flush_runtime_interval(connection, moment)

    def _open_active_period(self, connection: sqlite3.Connection, project_id: int, timestamp: str) -> None:
        """Fecha qualquer período aberto e abre um novo para o projeto informado."""
        connection.execute(
            "UPDATE project_active_periods SET end_at = ? WHERE end_at IS NULL;", (timestamp,)
        )
        connection.execute(
            "INSERT INTO project_active_periods (project_id, start_at, end_at) VALUES (?, ?, NULL);",
            (project_id, timestamp),
        )

    def _close_active_period(self, connection: sqlite3.Connection, project_id: int, timestamp: str) -> None:
        rows = connection.execute(
            "SELECT id, start_at FROM project_active_periods WHERE project_id = ? AND end_at IS NULL;",
            (project_id,),
        ).fetchall()
        end_dt = self._parse_local_datetime(timestamp)
        for row in rows:
            end_value = timestamp
            start_dt = self._parse_local_datetime(row["start_at"])
            if start_dt and end_dt and end_dt < start_dt:
                end_value = str(row["start_at"])
            connection.execute(
                "UPDATE project_active_periods SET end_at = ? WHERE id = ?;",
                (end_value, int(row["id"])),
            )

    def _record_presence(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            "INSERT OR IGNORE INTO app_presence (day) VALUES (?);",
            (datetime.now().strftime("%Y-%m-%d"),),
        )

    def _presence_days(self, connection: sqlite3.Connection) -> set[str]:
        return {row["day"] for row in connection.execute("SELECT day FROM app_presence;").fetchall()}

    @staticmethod
    def _parse_local_datetime(value: Any) -> datetime | None:
        if isinstance(value, datetime):
            return value.replace(tzinfo=None)
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value)).replace(tzinfo=None)
        except (TypeError, ValueError):
            return None

    def _stage_completion_timestamp(self, connection: sqlite3.Connection, project_id: int) -> str | None:
        row = connection.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status = 'Concluída' OR internal_percent >= 100 THEN 1 ELSE 0 END) AS completed,
                MAX(CASE WHEN status = 'Concluída' OR internal_percent >= 100
                         THEN COALESCE(completed_at, updated_at) END) AS completion_at
            FROM project_stages
            WHERE project_id = ?;
            """,
            (project_id,),
        ).fetchone()
        if not row or int(row["total"] or 0) <= 0:
            return None
        if int(row["completed"] or 0) != int(row["total"] or 0):
            return None
        return str(row["completion_at"] or "") or None

    def _backfill_start_date_period(
        self,
        connection: sqlite3.Connection,
        project_id: int,
        start_date: str,
        reference_timestamp: str,
    ) -> bool:
        """Estima horas anteriores ao cadastro quando a data de início é passada.

        Só cria o complemento quando a data é anterior ao dia atual e ainda não
        existe período começando naquela data. A jornada e os dias válidos continuam
        centralizados em app/worktime.py.
        """
        try:
            start_day = date.fromisoformat(str(start_date)[:10])
        except (TypeError, ValueError):
            return False
        reference_dt = self._parse_local_datetime(reference_timestamp) or datetime.now()
        if start_day >= reference_dt.date():
            return False

        first = connection.execute(
            "SELECT MIN(start_at) AS first_start FROM project_active_periods WHERE project_id = ?;",
            (project_id,),
        ).fetchone()
        first_start = str(first["first_start"] or "") if first else ""
        if first_start:
            first_dt = self._parse_local_datetime(first_start)
            if not first_dt or first_dt.date() <= start_day:
                return False
            end_timestamp = first_start
        else:
            end_timestamp = self._stage_completion_timestamp(connection, project_id) or reference_timestamp

        end_dt = self._parse_local_datetime(end_timestamp)
        if not end_dt or end_dt.date() <= start_day:
            return False
        connection.execute(
            "INSERT INTO project_active_periods (project_id, start_at, end_at) VALUES (?, ?, ?);",
            (project_id, f"{start_day.isoformat()}T00:00:00", end_timestamp),
        )
        self.add_activity(
            connection,
            project_id,
            "hora",
            "estimar",
            f"Horas estimadas desde {start_day.strftime('%d/%m/%Y')} pela jornada de trabalho.",
        )
        return True

    @staticmethod
    def _first_incomplete_stage_order(connection: sqlite3.Connection, project_id: int) -> int | None:
        row = connection.execute(
            """
            SELECT stage_order
            FROM project_stages
            WHERE project_id = ?
              AND internal_percent < 100
            ORDER BY stage_order ASC
            LIMIT 1;
            """,
            (project_id,),
        ).fetchone()
        return int(row["stage_order"]) if row else None

    def _start_next_stage(
        self, connection: sqlite3.Connection, project_id: int, completed_order: int, timestamp: str
    ) -> int | None:
        current = connection.execute(
            "SELECT status, internal_percent FROM project_stages WHERE project_id = ? AND stage_order = ?;",
            (project_id, completed_order),
        ).fetchone()
        if not current or not (str(current["status"]) == "Concluída" or float(current["internal_percent"] or 0) >= 100):
            return None
        # Ao concluir uma etapa fora de ordem, a próxima etapa de trabalho deve
        # continuar sendo a primeira pendente do fluxo, e não uma etapa ainda
        # posterior. Isso evita abrir vários relógios ao mesmo tempo.
        next_stage = connection.execute(
            """
            SELECT * FROM project_stages
            WHERE project_id = ?
              AND NOT (status = 'Concluída' OR internal_percent >= 100)
            ORDER BY stage_order ASC
            LIMIT 1;
            """,
            (project_id,),
        ).fetchone()
        if next_stage is None:
            return None
        next_order = int(next_stage["stage_order"] or 0)
        reset_idle_next = (
            str(next_stage["status"] or "") == "Em andamento"
            and float(next_stage["internal_percent"] or 0) <= 0
            and max(0, int(float(next_stage["active_seconds"] or 0))) == 0
            and str(next_stage["source"] or "") != REOPEN_STAGE_SOURCE
        )
        if str(next_stage["status"] or "") != "Em andamento":
            connection.execute(
                """
                UPDATE project_stages
                SET status = 'Em andamento', started_at = COALESCE(started_at, ?),
                    last_activity_at = ?, updated_at = ?
                WHERE id = ?;
                """,
                (timestamp, timestamp, timestamp, int(next_stage["id"])),
            )
            self.add_stage_observation(
                connection,
                int(next_stage["id"]),
                "fluxo automático",
                1.0,
                "Etapa iniciada automaticamente após a conclusão da etapa anterior.",
                {"previous_stage_order": completed_order, "stage_order": next_order},
            )
            self.add_activity(
                connection,
                project_id,
                "etapa",
                "iniciar",
                f"Etapa {next_order} iniciada automaticamente.",
            )
        elif reset_idle_next:
            connection.execute(
                """
                UPDATE project_stages
                SET started_at = ?, last_activity_at = ?, updated_at = ?
                WHERE id = ?;
                """,
                (timestamp, timestamp, timestamp, int(next_stage["id"])),
            )
            self.add_stage_observation(
                connection,
                int(next_stage["id"]),
                "fluxo automatico",
                1.0,
                "Etapa reposicionada como etapa atual sem herdar tempo anterior.",
                {"previous_stage_order": completed_order, "stage_order": next_order},
            )
        return next_order

    def _stage_distribution_elapsed_seconds(
        self,
        connection: sqlite3.Connection,
        project_id: int,
        start_at: str | None,
        end_at: str,
    ) -> int:
        if not start_at:
            return 0
        periods = [
            (row["start_at"], row["end_at"])
            for row in connection.execute(
                "SELECT start_at, end_at FROM project_active_periods WHERE project_id = ? ORDER BY id;",
                (project_id,),
            ).fetchall()
        ]
        supplemental_periods = [
            (row["started_at"], row["ended_at"])
            for row in connection.execute(
                """
                SELECT started_at, ended_at
                FROM work_sessions
                WHERE project_id = ?
                  AND source = 'app_open_extra'
                  AND active_seconds > 0
                ORDER BY id;
                """,
                (project_id,),
            ).fetchall()
        ]
        # Usa uma fonte neutra no cálculo. Se marcássemos o probe como
        # ``time_distribution``, _stage_time_seconds trataria o valor como já
        # registrado e retornaria zero antes de calcular o intervalo.
        probe = {
            "started_at": start_at,
            "completed_at": end_at,
            "status": "Concluída",
            "internal_percent": 100,
            "active_seconds": 0,
            "source": "distribution_probe",
        }
        return self._stage_time_seconds(
            probe, periods, supplemental_periods, self._presence_days(connection)
        )

    def _stage_completion_plan_locked(
        self,
        connection: sqlite3.Connection,
        project_id: int,
        stage_order: int,
        timestamp: str,
    ) -> dict[str, Any]:
        target = connection.execute(
            "SELECT * FROM project_stages WHERE project_id = ? AND stage_order = ?;",
            (project_id, stage_order),
        ).fetchone()
        if target is None:
            raise ValueError("Etapa não encontrada.")
        target_dict = dict(target)
        if str(target_dict.get("status") or "") == "Concluída" or float(target_dict.get("internal_percent") or 0) >= 100:
            return {
                "out_of_order": False,
                "elapsed_seconds": 0,
                "target": {
                    "stage_order": int(target_dict.get("stage_order") or stage_order),
                    "name": str(target_dict.get("name") or ""),
                },
                "missing_stages": [],
                "candidate_stages": [],
            }

        pending = [
            dict(row)
            for row in connection.execute(
                """
                SELECT * FROM project_stages
                WHERE project_id = ?
                  AND stage_order <= ?
                  AND NOT (status = 'Concluída' OR internal_percent >= 100)
                ORDER BY stage_order ASC;
                """,
                (project_id, stage_order),
            ).fetchall()
        ]
        missing = [row for row in pending if int(row.get("stage_order") or 0) < int(stage_order)]
        out_of_order = bool(missing)
        if not out_of_order:
            return {
                "out_of_order": False,
                "elapsed_seconds": 0,
                "target": {
                    "stage_order": int(target_dict.get("stage_order") or stage_order),
                    "name": str(target_dict.get("name") or ""),
                },
                "missing_stages": [],
                "candidate_stages": [],
            }

        first_pending = pending[0]
        anchor_at = str(first_pending.get("started_at") or "").strip()
        if not anchor_at:
            previous = connection.execute(
                """
                SELECT MAX(COALESCE(completed_at, updated_at)) AS completed_at
                FROM project_stages
                WHERE project_id = ? AND stage_order < ?
                  AND (status = 'Concluída' OR internal_percent >= 100);
                """,
                (project_id, int(first_pending.get("stage_order") or 0)),
            ).fetchone()
            anchor_at = str(previous["completed_at"] or "").strip() if previous else ""
        if not anchor_at:
            period = connection.execute(
                """
                SELECT start_at FROM project_active_periods
                WHERE project_id = ?
                ORDER BY id DESC LIMIT 1;
                """,
                (project_id,),
            ).fetchone()
            anchor_at = str(period["start_at"] or "").strip() if period else ""

        elapsed_seconds = self._stage_distribution_elapsed_seconds(
            connection, project_id, anchor_at or None, timestamp
        )
        def compact(row: dict[str, Any]) -> dict[str, Any]:
            return {
                "stage_order": int(row.get("stage_order") or 0),
                "name": str(row.get("name") or ""),
                "status": str(row.get("status") or ""),
                "time_seconds": max(0, int(float(row.get("active_seconds") or 0))),
            }

        candidates = [compact(row) for row in pending]
        return {
            "out_of_order": True,
            "elapsed_seconds": max(0, int(elapsed_seconds)),
            "anchor_at": anchor_at,
            "target": compact(target_dict),
            "missing_stages": [compact(row) for row in missing],
            "candidate_stages": candidates,
        }

    def stage_completion_plan(self, project_id: int, stage_order: int) -> dict[str, Any]:
        timestamp = now_iso()
        connection = self.connect()
        try:
            return self._stage_completion_plan_locked(
                connection, project_id, stage_order, timestamp
            )
        finally:
            connection.close()

    def _apply_stage_time_distribution(
        self,
        connection: sqlite3.Connection,
        project_id: int,
        stage_order: int,
        plan: dict[str, Any],
        selected_orders: list[Any],
        timestamp: str,
    ) -> None:
        allowed = {
            int(item.get("stage_order") or 0)
            for item in plan.get("candidate_stages", [])
            if isinstance(item, dict) and int(item.get("stage_order") or 0) > 0
        }
        selected = sorted(
            {parse_int(order, 0) for order in (selected_orders or []) if parse_int(order, 0) in allowed}
        )
        if int(stage_order) not in selected:
            raise ValueError("A etapa que está sendo concluída precisa participar da distribuição de tempo.")
        if not selected:
            raise ValueError("Selecione ao menos uma etapa para registrar o tempo.")

        elapsed = max(0, int(plan.get("elapsed_seconds") or 0))
        if elapsed > 0:
            base, remainder = divmod(elapsed, len(selected))
            shares = {order: base + (1 if idx < remainder else 0) for idx, order in enumerate(selected)}
            note = "Tempo distribuído ao concluir etapa fora de ordem."
            for order, seconds in shares.items():
                if seconds <= 0:
                    continue
                connection.execute(
                    """
                    INSERT INTO stage_time_records
                    (project_id, stage_order, start_at, end_at, seconds, source, note, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        project_id,
                        order,
                        plan.get("anchor_at") or None,
                        timestamp,
                        seconds,
                        TIME_DISTRIBUTION_SOURCE,
                        note,
                        timestamp,
                        timestamp,
                    ),
                )
                connection.execute(
                    """
                    UPDATE project_stages
                    SET active_seconds = active_seconds + ?, source = ?,
                        last_activity_at = ?, updated_at = ?
                    WHERE project_id = ? AND stage_order = ?;
                    """,
                    (seconds, TIME_DISTRIBUTION_SOURCE, timestamp, timestamp, project_id, order),
                )
                self.add_activity(
                    connection,
                    project_id,
                    "hora",
                    "distribuir",
                    f"{round(seconds / 3600.0, 2)} h distribuídas para a etapa {order}.",
                )

        # Reinicia o relógio somente na primeira etapa ainda pendente. Etapas
        # posteriores deixam de permanecer simultaneamente "Em andamento".
        pending_rows = connection.execute(
            """
            SELECT * FROM project_stages
            WHERE project_id = ?
              AND NOT (status = 'Concluída' OR internal_percent >= 100)
            ORDER BY stage_order ASC;
            """,
            (project_id,),
        ).fetchall()
        first_order = int(pending_rows[0]["stage_order"]) if pending_rows else 0
        for row in pending_rows:
            order = int(row["stage_order"] or 0)
            percent = float(row["internal_percent"] or 0)
            if order == first_order:
                connection.execute(
                    """
                    UPDATE project_stages
                    SET status = 'Em andamento', started_at = ?, last_activity_at = ?, updated_at = ?
                    WHERE id = ?;
                    """,
                    (timestamp, timestamp, timestamp, int(row["id"])),
                )
            elif str(row["status"] or "") == "Em andamento" and percent <= 0:
                connection.execute(
                    """
                    UPDATE project_stages
                    SET status = 'Não iniciada', started_at = NULL, updated_at = ?
                    WHERE id = ?;
                    """,
                    (timestamp, int(row["id"])),
                )

    def _sync_stage_timer(
        self, connection: sqlite3.Connection, project_id: int, timestamp: str
    ) -> None:
        completion_at = self._stage_completion_timestamp(connection, project_id)
        if completion_at:
            self._close_active_period(connection, project_id, completion_at)
            return
        project = connection.execute(
            "SELECT active FROM projects WHERE id = ?;", (project_id,)
        ).fetchone()
        if not project or not int(project["active"] or 0):
            return
        open_period = connection.execute(
            "SELECT 1 FROM project_active_periods WHERE project_id = ? AND end_at IS NULL LIMIT 1;",
            (project_id,),
        ).fetchone()
        if open_period is None:
            self._open_active_period(connection, project_id, timestamp)

    def _reconcile_existing_stage_flow_and_time(self, connection: sqlite3.Connection) -> None:
        """Corrige projetos existentes sem alterar seus percentuais.

        - inicia a primeira etapa pendente quando todas as anteriores já estão concluídas;
        - completa o histórico desde uma data inicial passada;
        - encerra o relógio na data real em que todas as etapas foram concluídas.
        """
        timestamp = now_iso()
        rows = connection.execute(
            "SELECT id, start_date FROM projects ORDER BY id;"
        ).fetchall()
        for row in rows:
            project_id = int(row["id"] or 0)
            stages = connection.execute(
                "SELECT * FROM project_stages WHERE project_id = ? ORDER BY stage_order ASC;",
                (project_id,),
            ).fetchall()
            first_incomplete = next(
                (stage for stage in stages if not (str(stage["status"]) == "Concluída" or float(stage["internal_percent"] or 0) >= 100)),
                None,
            )
            has_active = any(str(stage["status"] or "") == "Em andamento" for stage in stages)
            if first_incomplete is not None and not has_active:
                previous_completed = any(
                    int(stage["stage_order"] or 0) < int(first_incomplete["stage_order"] or 0)
                    and (str(stage["status"]) == "Concluída" or float(stage["internal_percent"] or 0) >= 100)
                    for stage in stages
                )
                if previous_completed:
                    connection.execute(
                        """
                        UPDATE project_stages
                        SET status = 'Em andamento', started_at = COALESCE(started_at, ?),
                            last_activity_at = ?, updated_at = ?
                        WHERE id = ?;
                        """,
                        (timestamp, timestamp, timestamp, int(first_incomplete["id"])),
                    )
            self._backfill_start_date_period(
                connection, project_id, str(row["start_date"] or ""), timestamp
            )
            self._sync_stage_timer(connection, project_id, timestamp)

    def _project_active_seconds(self, connection: sqlite3.Connection, project_id: int) -> int:
        completion_at = self._stage_completion_timestamp(connection, project_id)
        periods = [
            (row["start_at"], row["end_at"] or completion_at)
            for row in connection.execute(
                "SELECT start_at, end_at FROM project_active_periods WHERE project_id = ? ORDER BY id;",
                (project_id,),
            ).fetchall()
        ]
        scheduled = project_worked_seconds(periods, self._presence_days(connection)) if periods else 0
        supplemental = int(
            connection.execute(
                "SELECT COALESCE(SUM(active_seconds), 0) AS seconds FROM work_sessions "
                "WHERE project_id = ? AND source IN ('app_open_extra', 'revision_adjustment');",
                (project_id,),
            ).fetchone()["seconds"] or 0
        )
        return max(0, int(scheduled) + supplemental)

    def _register_historical_period(self, connection: sqlite3.Connection, project_id: int, start_date: str, end_date: str) -> bool:
        """Projeto cadastrado como concluído: cria um período fechado [início, entrega] e
        marca os sábados do intervalo como presentes, para estimar as horas pela jornada
        (seg-sáb, sem domingo) a partir das datas da legenda do desenho."""
        try:
            start = date.fromisoformat(str(start_date)[:10])
            end = date.fromisoformat(str(end_date)[:10])
        except ValueError:
            return False
        if end < start:
            return False
        connection.execute(
            "INSERT INTO project_active_periods (project_id, start_at, end_at) VALUES (?, ?, ?);",
            (project_id, f"{start.isoformat()}T00:00:00", f"{end.isoformat()}T23:59:59"),
        )
        day = start
        while day <= end:
            if day.weekday() == 5:  # sábado conta no cálculo histórico
                connection.execute("INSERT OR IGNORE INTO app_presence (day) VALUES (?);", (day.isoformat(),))
            day += timedelta(days=1)
        return True

    def reset_operational_data(self) -> dict[str, Any]:
        """Apaga todos os dados operacionais (projetos, desenhos, histórico, dados da
        era API), preservando configurações e templates de etapas. Faz backup antes."""
        self._backup_existing_database()
        removed: dict[str, int] = {}
        with self._lock, self.session() as connection:
            existing = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table';"
                ).fetchall()
            }
            for table in self.OPERATIONAL_TABLES:
                if table not in existing:
                    continue
                count = connection.execute(f"SELECT COUNT(*) AS n FROM {table};").fetchone()["n"]
                connection.execute(f"DELETE FROM {table};")
                removed[table] = int(count or 0)
            connection.execute(
                "DELETE FROM sqlite_sequence WHERE name IN "
                "('projects','project_stages','drawings','issues','activity_history','work_sessions','stage_time_records','stage_observations','project_files','project_indicator_assemblies','project_revision_history');"
            )
            for key in ("last_tekla_status", "last_tekla_sync"):
                connection.execute("UPDATE settings SET value = '' WHERE key = ?;", (key,))
        return {"removed": removed, "total": sum(removed.values())}

    def set_project_active(self, project_id: int) -> dict[str, Any]:
        timestamp = now_iso()
        with self._lock, self.session() as connection:
            row = connection.execute("SELECT code FROM projects WHERE id = ?;", (project_id,)).fetchone()
            if row is None:
                raise ValueError("Projeto não encontrado.")
            self._heartbeat_runtime_session(connection, reference=timestamp)
            connection.execute("UPDATE projects SET active = 0;")
            connection.execute("UPDATE projects SET active = 1, updated_at = ? WHERE id = ?;", (timestamp, project_id))
            # Trocar o projeto ativo encerra o período anterior. Projetos já
            # concluídos podem ser consultados, mas não reabrem o relógio.
            connection.execute("UPDATE project_active_periods SET end_at = ? WHERE end_at IS NULL;", (timestamp,))
            if not self._stage_completion_timestamp(connection, project_id):
                connection.execute(
                    "INSERT INTO project_active_periods (project_id, start_at, end_at) VALUES (?, ?, NULL);",
                    (project_id, timestamp),
                )
                self._start_runtime_session(connection, project_id, timestamp)
            else:
                self._stop_runtime_session(connection, timestamp)
            self.add_activity(connection, project_id, "projeto", "ativar", f"Projeto {row['code']} definido como ativo.")
        return self.get_project(project_id) or {}

    def set_project_status(self, project_id: int, status: str) -> dict[str, Any]:
        allowed = {"Planejamento", "Em andamento", "Pausado", "Concluído", "Arquivado"}
        if status not in allowed:
            raise ValueError("Status inválido.")
        timestamp = now_iso()
        with self._lock, self.session() as connection:
            connection.execute(
                """
                UPDATE projects
                SET status = ?, completed_at = CASE WHEN ? = 'Concluído' THEN ? ELSE completed_at END,
                    archived_at = CASE WHEN ? = 'Arquivado' THEN ? ELSE archived_at END,
                    updated_at = ?
                WHERE id = ?;
                """,
                (status, status, timestamp, status, timestamp, timestamp, project_id),
            )
            # O status/finalização administrativa é independente do relógio das etapas.
            # A contagem só é encerrada quando todas as etapas ficam concluídas.
            self._sync_stage_timer(connection, project_id, timestamp)
            self.add_activity(connection, project_id, "projeto", "status", f"Status alterado para {status}.")
        return self.get_project(project_id) or {}

    def get_project_stages(self, project_id: int, connection: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
        owns_connection = connection is None
        conn = connection or self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM project_stages WHERE project_id = ? ORDER BY stage_order ASC;",
                (project_id,),
            ).fetchall()
            return self._with_stage_time_metrics([dict(row) for row in rows], conn, project_id)
        finally:
            if owns_connection:
                conn.close()

    def _with_stage_time_metrics(
        self,
        stages: list[dict[str, Any]],
        connection: sqlite3.Connection,
        project_id: int,
    ) -> list[dict[str, Any]]:
        periods = [
            (row["start_at"], row["end_at"])
            for row in connection.execute(
                "SELECT start_at, end_at FROM project_active_periods WHERE project_id = ? ORDER BY id;",
                (project_id,),
            ).fetchall()
        ]
        supplemental_periods = [
            (row["started_at"], row["ended_at"])
            for row in connection.execute(
                """
                SELECT started_at, ended_at
                FROM work_sessions
                WHERE project_id = ?
                  AND source = 'app_open_extra'
                  AND active_seconds > 0
                ORDER BY id;
                """,
                (project_id,),
            ).fetchall()
        ]
        presence_days = self._presence_days(connection)
        for stage in stages:
            seconds = self._stage_time_seconds(stage, periods, supplemental_periods, presence_days)
            stage["time_seconds"] = seconds
        total_seconds = sum(max(0, int(stage.get("time_seconds") or 0)) for stage in stages)
        for stage in stages:
            seconds = max(0, int(stage.get("time_seconds") or 0))
            stage["time_percent"] = round((seconds * 100.0) / total_seconds, 2) if total_seconds else 0.0
        return stages

    def _stage_time_seconds(
        self,
        stage: dict[str, Any],
        project_periods: list[tuple[Any, Any]],
        supplemental_periods: list[tuple[Any, Any]],
        presence_days: set[str],
    ) -> int:
        recorded_seconds = max(0, int(float(stage.get("active_seconds") or 0)))
        adjustment_seconds = int(float(stage.get("time_adjustment_seconds") or 0))

        def adjusted(seconds: int) -> int:
            return max(0, int(seconds) + adjustment_seconds)

        source = str(stage.get("source") or "")
        reopened = source == REOPEN_STAGE_SOURCE
        distributed = source == TIME_DISTRIBUTION_SOURCE
        completed = str(stage.get("status") or "") == "Concluída" or float(stage.get("internal_percent") or 0) >= 100
        if recorded_seconds > 0 and not reopened and not distributed:
            return adjusted(recorded_seconds)
        if distributed and completed:
            return adjusted(recorded_seconds)

        started_at = self._parse_local_datetime(stage.get("started_at"))
        if started_at is None:
            return adjusted(recorded_seconds if (reopened or distributed) else 0)

        completed_at = self._parse_local_datetime(stage.get("completed_at")) if completed else None
        if completed and completed_at is None:
            completed_at = self._parse_local_datetime(stage.get("updated_at") or stage.get("last_activity_at"))

        clipped_periods = self._clip_stage_periods(started_at, completed_at, project_periods)
        scheduled_seconds = project_worked_seconds(clipped_periods, presence_days)
        extra_seconds = sum(
            outside_schedule_seconds(start, end, presence_days)
            for start, end in self._clip_stage_periods(started_at, completed_at, supplemental_periods)
            if end is not None
        )
        return adjusted((recorded_seconds if (reopened or distributed) else 0) + scheduled_seconds + extra_seconds)

    def _clip_stage_periods(
        self,
        started_at: datetime,
        completed_at: datetime | None,
        periods: list[tuple[Any, Any]],
    ) -> list[tuple[datetime, datetime | None]]:
        clipped_periods: list[tuple[datetime, datetime | None]] = []
        for period_start_raw, period_end_raw in periods:
            period_start = self._parse_local_datetime(period_start_raw)
            if period_start is None:
                continue
            period_end = self._parse_local_datetime(period_end_raw)
            start = max(started_at, period_start)
            if completed_at is not None and start >= completed_at:
                continue
            if period_end is not None:
                end = min(period_end, completed_at) if completed_at is not None else period_end
                if end > start:
                    clipped_periods.append((start, end))
            elif completed_at is not None:
                if completed_at > start:
                    clipped_periods.append((start, completed_at))
            else:
                clipped_periods.append((start, None))
        return clipped_periods

    def update_stage(self, project_id: int, stage_order: int, data: dict[str, Any]) -> dict[str, Any]:
        timestamp = now_iso()
        with self._lock, self.session() as connection:
            self._heartbeat_runtime_session(connection, reference=timestamp)
            row = connection.execute(
                "SELECT * FROM project_stages WHERE project_id = ? AND stage_order = ?;",
                (project_id, stage_order),
            ).fetchone()
            if row is None:
                raise ValueError("Etapa não encontrada.")
            current = dict(row)
            percent = clamp(parse_float(data.get("internal_percent", current["internal_percent"])))
            status = str(data.get("status") or current["status"])
            if status == "Concluída":
                percent = 100.0
            if percent >= 100:
                status = "Concluída"
            if percent > 0 and status == "Não iniciada":
                status = "Em andamento"
            notes = str(data.get("notes", current.get("notes") or ""))
            source = str(data.get("source") or current.get("source") or "manual")
            confidence_default = 1.0 if data.get("source") == "manual" else current["confidence"]
            confidence = max(0.0, min(1.0, parse_float(data.get("confidence", confidence_default))))
            started_at = current["started_at"] or (timestamp if percent > 0 else None)
            completed_at = (
                timestamp if status == "Concluída" and not current["completed_at"]
                else current["completed_at"] if status == "Concluída"
                else None
            )
            completion_plan: dict[str, Any] | None = None
            distribution_orders: list[Any] | None = None
            if percent >= 100:
                completion_plan = self._stage_completion_plan_locked(
                    connection, project_id, stage_order, timestamp
                )
                if completion_plan.get("out_of_order"):
                    if "time_distribution_orders" not in data:
                        raise ValueError(
                            "Esta etapa está sendo concluída fora de ordem. Escolha como distribuir o tempo antes de concluir."
                        )
                    distribution_orders = list(data.get("time_distribution_orders") or [])
                first_incomplete_order = self._first_incomplete_stage_order(connection, project_id)
                empty_out_of_order = (
                    first_incomplete_order is not None
                    and int(stage_order) > first_incomplete_order
                    and float(current.get("internal_percent") or 0) <= 0
                    and max(0, int(float(current.get("active_seconds") or 0))) == 0
                    and str(current.get("source") or "") != REOPEN_STAGE_SOURCE
                )
                if empty_out_of_order:
                    started_at = timestamp
                    completed_at = timestamp
            manual_confirmed = 1 if data.get("manual_confirmed", True) else int(current["manual_confirmed"])
            connection.execute(
                """
                UPDATE project_stages
                SET status = ?, internal_percent = ?, source = ?, confidence = ?, started_at = ?,
                    last_activity_at = ?, completed_at = ?, notes = ?, manual_confirmed = ?, updated_at = ?
                WHERE project_id = ? AND stage_order = ?;
                """,
                (
                    status,
                    percent,
                    source,
                    confidence,
                    started_at,
                    timestamp,
                    completed_at,
                    notes,
                    manual_confirmed,
                    timestamp,
                    project_id,
                    stage_order,
                ),
            )
            if completion_plan and completion_plan.get("out_of_order"):
                self._apply_stage_time_distribution(
                    connection,
                    project_id,
                    stage_order,
                    completion_plan,
                    distribution_orders or [],
                    timestamp,
                )

            stage_id = int(current["id"])
            self.add_stage_observation(
                connection,
                stage_id,
                source,
                confidence,
                str(data.get("message") or "Etapa atualizada manualmente."),
                data,
            )
            self.add_activity(connection, project_id, "etapa", "atualizar", f"Etapa {stage_order} atualizada para {percent:.0f}%.")
            if status == "Concluída":
                self._start_next_stage(connection, project_id, stage_order, timestamp)
            self._sync_stage_timer(connection, project_id, timestamp)
            if self._stage_completion_timestamp(connection, project_id):
                self._stop_runtime_session(connection, timestamp)
        return self.get_project(project_id) or {}

    def reopen_stages(self, project_id: int, stage_orders: list[Any]) -> dict[str, Any]:
        clean_orders = sorted({parse_int(order, 0) for order in stage_orders or [] if parse_int(order, 0) > 0})
        if not clean_orders:
            raise ValueError("Selecione ao menos uma etapa concluida para reabrir.")
        timestamp = now_iso()
        with self._lock, self.session() as connection:
            self._heartbeat_runtime_session(connection, reference=timestamp)
            project_row = connection.execute("SELECT * FROM projects WHERE id = ?;", (project_id,)).fetchone()
            if project_row is None:
                raise ValueError("Projeto nao encontrado.")
            placeholders = ",".join("?" for _ in clean_orders)
            rows = connection.execute(
                f"""
                SELECT *
                FROM project_stages
                WHERE project_id = ? AND stage_order IN ({placeholders})
                ORDER BY stage_order ASC;
                """,
                (project_id, *clean_orders),
            ).fetchall()
            if not rows:
                raise ValueError("Etapa nao encontrada.")
            stages_with_time = self._with_stage_time_metrics([dict(row) for row in rows], connection, project_id)
            reopened: list[int] = []
            for stage in stages_with_time:
                done = str(stage.get("status") or "") == "Concluída" or float(stage.get("internal_percent") or 0) >= 100
                if not done:
                    continue
                baseline_seconds = max(0, int(stage.get("time_seconds") or stage.get("active_seconds") or 0))
                connection.execute(
                    """
                    UPDATE project_stages
                    SET status = 'Em andamento',
                        internal_percent = 0,
                        source = ?,
                        confidence = 1.0,
                        started_at = ?,
                        last_activity_at = ?,
                        completed_at = NULL,
                        active_seconds = ?,
                        manual_confirmed = 0,
                        expected_total = 0,
                        observed_done = 0,
                        updated_at = ?
                    WHERE id = ?;
                    """,
                    (
                        REOPEN_STAGE_SOURCE,
                        timestamp,
                        timestamp,
                        baseline_seconds,
                        timestamp,
                        int(stage["id"]),
                    ),
                )
                reopened.append(int(stage["stage_order"]))
                self.add_stage_observation(
                    connection,
                    int(stage["id"]),
                    REOPEN_STAGE_SOURCE,
                    1.0,
                    "Etapa reaberta para registrar tempo adicional.",
                    {"stage_order": int(stage["stage_order"]), "baseline_seconds": baseline_seconds},
                )
            if not reopened:
                raise ValueError("Selecione etapas que ja estejam concluidas.")
            connection.execute(
                """
                UPDATE projects
                SET status = CASE WHEN status = 'Arquivado' THEN status ELSE 'Em andamento' END,
                    completed_at = NULL,
                    updated_at = ?
                WHERE id = ?;
                """,
                (timestamp, project_id),
            )
            self.add_activity(
                connection,
                project_id,
                "etapa",
                "reabrir",
                f"Etapa(s) reaberta(s): {', '.join(str(order) for order in reopened)}.",
            )
            self._sync_stage_timer(connection, project_id, timestamp)
            self._heartbeat_runtime_session(connection, reference=timestamp)
        return self.get_project(project_id) or {}

    @staticmethod
    def _normalize_stage_list(stages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Normaliza uma lista de etapas do editor: renumera 1..N e valida nome/peso."""
        clean: list[dict[str, Any]] = []
        order = 0
        for item in stages or []:
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            order += 1
            clean.append(
                {
                    "stage_order": order,
                    "name": name,
                    "weight": max(0.0, parse_float(item.get("weight"), 0.0)),
                }
            )
        if not clean:
            raise ValueError("Informe ao menos uma etapa.")
        return clean

    def replace_project_stages(self, project_id: int, stages: list[dict[str, Any]]) -> dict[str, Any]:
        """Substitui as etapas de UM projeto (edição). Preserva progresso por stage_order."""
        clean = self._normalize_stage_list(stages)
        timestamp = now_iso()
        new_orders = {item["stage_order"] for item in clean}
        with self._lock, self.session() as connection:
            if connection.execute("SELECT 1 FROM projects WHERE id = ?;", (project_id,)).fetchone() is None:
                raise ValueError("Projeto não encontrado.")
            existing = {
                int(row["stage_order"])
                for row in connection.execute(
                    "SELECT stage_order FROM project_stages WHERE project_id = ?;", (project_id,)
                ).fetchall()
            }
            for order in existing - new_orders:
                connection.execute(
                    "DELETE FROM project_stages WHERE project_id = ? AND stage_order = ?;", (project_id, order)
                )
            for item in clean:
                if item["stage_order"] in existing:
                    connection.execute(
                        "UPDATE project_stages SET name = ?, weight = ?, updated_at = ? WHERE project_id = ? AND stage_order = ?;",
                        (item["name"], item["weight"], timestamp, project_id, item["stage_order"]),
                    )
                else:
                    connection.execute(
                        """
                        INSERT INTO project_stages
                        (project_id, stage_order, name, weight, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?);
                        """,
                        (project_id, item["stage_order"], item["name"], item["weight"], timestamp, timestamp),
                    )
            self.add_activity(
                connection, project_id, "etapa", "editar", f"Etapas do projeto atualizadas ({len(clean)} etapas)."
            )
        return self.get_project(project_id) or {}

    def get_stage_template(self, program: str, mode: str) -> dict[str, Any]:
        program = program if program in PROGRAMS else DEFAULT_PROGRAM
        mode = mode if mode in STAGE_MODES else DEFAULT_STAGE_MODE
        with self._lock, self.session() as connection:
            return {"program": program, "mode": mode, "stages": self._template_stages(connection, program, mode)}

    def replace_stage_template(self, program: str, mode: str, stages: list[dict[str, Any]]) -> dict[str, Any]:
        """Substitui o TEMPLATE de (programa × nível) — padrão para novos projetos."""
        if program not in PROGRAMS:
            raise ValueError("Programa inválido.")
        if mode not in STAGE_MODES:
            raise ValueError("Nível de etapas inválido.")
        clean = self._normalize_stage_list(stages)
        timestamp = now_iso()
        with self._lock, self.session() as connection:
            connection.execute(
                "DELETE FROM stage_templates WHERE program = ? AND mode = ?;", (program, mode)
            )
            for item in clean:
                connection.execute(
                    """
                    INSERT INTO stage_templates
                    (program, mode, stage_order, name, weight, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?);
                    """,
                    (program, mode, item["stage_order"], item["name"], item["weight"], timestamp, timestamp),
                )
            self.add_activity(
                connection, None, "etapa", "template", f"Template de etapas atualizado: {program} · {mode} ({len(clean)} etapas)."
            )
        return self.get_stage_template(program, mode)

    def apply_stage_update(self, connection: sqlite3.Connection, project_id: int, update: dict[str, Any]) -> None:
        stage_order = int(update["stage_order"])
        row = connection.execute(
            "SELECT * FROM project_stages WHERE project_id = ? AND stage_order = ?;",
            (project_id, stage_order),
        ).fetchone()
        if row is None:
            return
        current = dict(row)
        self.add_stage_observation(
            connection,
            int(current["id"]),
            str(update.get("source") or "automática"),
            parse_float(update.get("confidence"), 0.0),
            str(update.get("message") or "Observação automática."),
            update,
        )
        if current.get("manual_confirmed") and float(current.get("internal_percent") or 0) >= 100:
            return
        new_percent = max(float(current["internal_percent"] or 0), clamp(parse_float(update.get("internal_percent"))))
        if new_percent <= float(current["internal_percent"] or 0) and str(current["status"]) == "Concluída":
            return
        status = str(update.get("status") or current["status"])
        if new_percent >= 100:
            status = "Concluída"
        elif new_percent > 0 and current["status"] == "Não iniciada":
            status = "Em andamento"
        timestamp = now_iso()
        connection.execute(
            """
            UPDATE project_stages
            SET internal_percent = ?, status = ?, source = ?, confidence = MAX(confidence, ?),
                started_at = COALESCE(started_at, ?), last_activity_at = ?,
                completed_at = CASE WHEN ? = 'Concluída' THEN COALESCE(completed_at, ?) ELSE completed_at END,
                updated_at = ?
            WHERE id = ?;
            """,
            (
                new_percent,
                status,
                str(update.get("source") or "automática"),
                parse_float(update.get("confidence"), 0.0),
                timestamp,
                timestamp,
                status,
                timestamp,
                timestamp,
                int(current["id"]),
            ),
        )
        if status == "Concluída":
            self._start_next_stage(connection, project_id, stage_order, timestamp)
        self._sync_stage_timer(connection, project_id, timestamp)

    def add_stage_observation(
        self,
        connection: sqlite3.Connection,
        stage_id: int,
        source: str,
        confidence: float,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO stage_observations (project_stage_id, source, confidence, message, details_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?);
            """,
            (stage_id, source, confidence, message, to_json(details or {}), now_iso()),
        )

    def list_issues(self, project_id: int, connection: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
        owns_connection = connection is None
        conn = connection or self.connect()
        try:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM issues WHERE project_id = ? ORDER BY status ASC, due_date ASC, id DESC;",
                    (project_id,),
                ).fetchall()
            ]
        finally:
            if owns_connection:
                conn.close()

    def save_issue(self, project_id: int, data: dict[str, Any], issue_id: int | None = None) -> dict[str, Any]:
        timestamp = now_iso()
        payload = {field: data.get(field) for field in ISSUE_FIELDS if field in data}
        payload["title"] = str(payload.get("title") or "").strip()
        if not payload["title"]:
            raise ValueError("Título da pendência é obrigatório.")
        with self._lock, self.session() as connection:
            if issue_id:
                updates = {key: payload[key] for key in payload}
                updates["updated_at"] = timestamp
                assignments = ", ".join(f"{key} = ?" for key in updates)
                connection.execute(f"UPDATE issues SET {assignments} WHERE id = ? AND project_id = ?;", [*updates.values(), issue_id, project_id])
                action = "editar"
                message = "Pendência atualizada."
            else:
                columns = ["project_id", "title", "description", "priority", "status", "responsible", "due_date", "drawing_id", "created_at", "updated_at"]
                values = [
                    project_id,
                    payload["title"],
                    str(payload.get("description") or ""),
                    str(payload.get("priority") or "Média"),
                    str(payload.get("status") or "Aberta"),
                    str(payload.get("responsible") or ""),
                    str(payload.get("due_date") or ""),
                    payload.get("drawing_id") or None,
                    timestamp,
                    timestamp,
                ]
                connection.execute(f"INSERT INTO issues ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)});", values)
                action = "criar"
                message = "Pendência criada."
            self.add_activity(connection, project_id, "pendência", action, message, payload)
        return self.get_project(project_id) or {}

    def delete_issue(self, project_id: int, issue_id: int) -> dict[str, Any]:
        with self._lock, self.session() as connection:
            connection.execute("DELETE FROM issues WHERE id = ? AND project_id = ?;", (issue_id, project_id))
            self.add_activity(connection, project_id, "pendência", "excluir", "Pendência excluída.")
        return self.get_project(project_id) or {}

    def list_files(self, project_id: int, connection: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
        owns_connection = connection is None
        conn = connection or self.connect()
        try:
            return [dict(row) for row in conn.execute("SELECT * FROM project_files WHERE project_id = ? ORDER BY updated_at DESC;", (project_id,)).fetchall()]
        finally:
            if owns_connection:
                conn.close()

    def save_file(self, project_id: int, data: dict[str, Any], file_id: int | None = None) -> dict[str, Any]:
        timestamp = now_iso()
        payload = {field: str(data.get(field) or "").strip() for field in FILE_FIELDS}
        if not payload["file_path"]:
            raise ValueError("Caminho do arquivo é obrigatório.")
        if not payload["name"]:
            payload["name"] = Path(payload["file_path"]).name
        size = 0
        try:
            path = Path(payload["file_path"])
            size = path.stat().st_size if path.exists() and path.is_file() else 0
        except OSError:
            size = 0
        with self._lock, self.session() as connection:
            if file_id:
                connection.execute(
                    """
                    UPDATE project_files
                    SET name = ?, file_path = ?, file_type = ?, description = ?, size_bytes = ?, updated_at = ?
                    WHERE id = ? AND project_id = ?;
                    """,
                    (payload["name"], payload["file_path"], payload["file_type"], payload["description"], size, timestamp, file_id, project_id),
                )
                action = "editar"
            else:
                connection.execute(
                    """
                    INSERT INTO project_files
                    (project_id, name, file_path, file_type, description, size_bytes, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (project_id, payload["name"], payload["file_path"], payload["file_type"], payload["description"], size, timestamp, timestamp),
                )
                action = "criar"
            self.add_activity(connection, project_id, "arquivo", action, f"Arquivo vinculado: {payload['name']}", payload)
        return self.get_project(project_id) or {}

    def delete_file(self, project_id: int, file_id: int) -> dict[str, Any]:
        with self._lock, self.session() as connection:
            connection.execute("DELETE FROM project_files WHERE id = ? AND project_id = ?;", (file_id, project_id))
            self.add_activity(connection, project_id, "arquivo", "excluir", "Arquivo desvinculado.")
        return self.get_project(project_id) or {}

    def list_drawings(self, project_id: int, connection: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
        owns_connection = connection is None
        conn = connection or self.connect()
        try:
            return [dict(row) for row in conn.execute("SELECT * FROM drawings WHERE project_id = ? ORDER BY code ASC, id DESC;", (project_id,)).fetchall()]
        finally:
            if owns_connection:
                conn.close()

    def project_sheet_total(
        self,
        project_id: int,
        connection: sqlite3.Connection | None = None,
        project: dict[str, Any] | None = None,
        detail_progress: dict[str, Any] | None = None,
    ) -> int:
        """Quantidade de folhas conhecida pelas fontes disponíveis.

        Mantém o total já importado do PDF/desenhos e, antes da importação,
        acompanha continuamente a maior margem temporária reconhecida pelo
        Progresso de detalhamento.
        """
        owns_connection = connection is None
        conn = connection or self.connect()
        try:
            total = 0
            drawing_rows = conn.execute(
                """
                SELECT drawing_key, code, drawing_type, sheet_count
                FROM drawings
                WHERE project_id = ?;
                """,
                (project_id,),
            ).fetchall()
            multidrawing_count = 0
            for row in drawing_rows:
                drawing_type = row["drawing_type"]
                drawing_key = row["drawing_key"]
                if not is_multidrawing_type(drawing_type) and not is_multidrawing_type(drawing_key):
                    continue
                multidrawing_count += 1
                total = max(total, parse_int(row["sheet_count"]))
                total = max(total, sheet_number_from_text(row["code"], drawing_key))
            total = max(total, multidrawing_count)

            indicator_row = conn.execute(
                "SELECT page_count FROM project_indicators WHERE project_id = ?;",
                (project_id,),
            ).fetchone()
            if indicator_row:
                indicator_total = parse_int(indicator_row["page_count"], 0)
                if indicator_total > 0:
                    return indicator_total

            if detail_progress is None:
                if project is None:
                    project_row = conn.execute(
                        "SELECT * FROM projects WHERE id = ?;", (project_id,)
                    ).fetchone()
                    project = dict(project_row) if project_row else {}
                detail_progress = self._detail_progress_for_project(project or {})
            if detail_progress:
                total = max(total, parse_int(detail_progress.get("sheet_total"), 0))

            return max(0, total)
        finally:
            if owns_connection:
                conn.close()

    def save_drawing(self, project_id: int, data: dict[str, Any], drawing_id: int | None = None) -> dict[str, Any]:
        timestamp = now_iso()
        payload = {field: data.get(field) for field in DRAWING_FIELDS if field in data}
        payload["code"] = str(payload.get("code") or "").strip()
        payload["title"] = str(payload.get("title") or "").strip()
        if not payload["code"] and not payload["drawing_key"]:
            raise ValueError("Informe o código ou chave do desenho.")
        with self._lock, self.session() as connection:
            if drawing_id:
                updates = {key: payload[key] for key in payload}
                updates["updated_at"] = timestamp
                assignments = ", ".join(f"{key} = ?" for key in updates)
                connection.execute(f"UPDATE drawings SET {assignments} WHERE id = ? AND project_id = ?;", [*updates.values(), drawing_id, project_id])
                action = "editar"
            else:
                columns = [
                    "project_id",
                    "drawing_key",
                    "code",
                    "title",
                    "revision",
                    "drawing_type",
                    "sheet_count",
                    "view_count",
                    "object_count",
                    "status",
                    "created_at",
                    "updated_at",
                ]
                values = [
                    project_id,
                    str(payload.get("drawing_key") or ""),
                    payload["code"],
                    payload["title"],
                    str(payload.get("revision") or ""),
                    str(payload.get("drawing_type") or ""),
                    parse_int(payload.get("sheet_count")),
                    parse_int(payload.get("view_count")),
                    parse_int(payload.get("object_count")),
                    str(payload.get("status") or "Em andamento"),
                    timestamp,
                    timestamp,
                ]
                connection.execute(f"INSERT INTO drawings ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)});", values)
                action = "criar"
            self.add_activity(connection, project_id, "desenho", action, "Desenho atualizado.", payload)
        return self.get_project(project_id) or {}

    def delete_drawing(self, project_id: int, drawing_id: int) -> dict[str, Any]:
        with self._lock, self.session() as connection:
            connection.execute("DELETE FROM drawings WHERE id = ? AND project_id = ?;", (drawing_id, project_id))
            self.add_activity(connection, project_id, "desenho", "excluir", "Desenho excluído.")
        return self.get_project(project_id) or {}

    def list_time_records(self, project_id: int, connection: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
        owns_connection = connection is None
        conn = connection or self.connect()
        try:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM stage_time_records WHERE project_id = ? ORDER BY created_at DESC LIMIT 100;",
                    (project_id,),
                ).fetchall()
            ]
        finally:
            if owns_connection:
                conn.close()

    def record_time(self, project_id: int, stage_order: int, seconds: int, note: str = "", source: str = "manual") -> dict[str, Any]:
        seconds = max(0, int(seconds))
        if seconds <= 0:
            raise ValueError("Informe um tempo maior que zero.")
        timestamp = now_iso()
        with self._lock, self.session() as connection:
            connection.execute(
                """
                INSERT INTO stage_time_records
                (project_id, stage_order, seconds, source, note, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?);
                """,
                (project_id, stage_order, seconds, source, note, timestamp, timestamp),
            )
            connection.execute(
                """
                UPDATE project_stages
                SET active_seconds = active_seconds + ?, last_activity_at = ?, updated_at = ?
                WHERE project_id = ? AND stage_order = ?;
                """,
                (seconds, timestamp, timestamp, project_id, stage_order),
            )
            connection.execute(
                """
                INSERT INTO work_sessions (project_id, active_seconds, source, note, created_at)
                VALUES (?, ?, ?, ?, ?);
                """,
                (project_id, seconds, source, note, timestamp),
            )
            self.add_activity(connection, project_id, "hora", "registrar", f"{round(seconds / 3600.0, 2)} h registradas na etapa {stage_order}.")
        return self.get_project(project_id) or {}

    def adjust_stage_times(self, project_id: int, stages: list[dict[str, Any]]) -> dict[str, Any]:
        requested: dict[int, int] = {}
        for item in stages or []:
            if not isinstance(item, dict):
                continue
            order = parse_int(item.get("stage_order"), 0)
            if order <= 0:
                continue
            if "seconds" in item:
                seconds = parse_int(item.get("seconds"), 0)
            else:
                hours = max(0.0, parse_float(item.get("hours"), 0.0))
                minutes = max(0.0, parse_float(item.get("minutes"), 0.0))
                seconds = int((hours * 3600) + (minutes * 60))
            requested[order] = max(0, int(seconds))
        if not requested:
            raise ValueError("Informe ao menos uma etapa para ajustar.")

        timestamp = now_iso()
        with self._lock, self.session() as connection:
            self._heartbeat_runtime_session(connection, reference=timestamp)
            if connection.execute("SELECT 1 FROM projects WHERE id = ?;", (project_id,)).fetchone() is None:
                raise ValueError("Projeto não encontrado.")

            rows = connection.execute(
                "SELECT * FROM project_stages WHERE project_id = ? ORDER BY stage_order ASC;",
                (project_id,),
            ).fetchall()
            row_by_order = {int(row["stage_order"]): dict(row) for row in rows}
            missing = [order for order in requested if order not in row_by_order]
            if missing:
                raise ValueError(f"Etapa {missing[0]} não encontrada.")

            periods = [
                (row["start_at"], row["end_at"])
                for row in connection.execute(
                    "SELECT start_at, end_at FROM project_active_periods WHERE project_id = ? ORDER BY id;",
                    (project_id,),
                ).fetchall()
            ]
            supplemental_periods = [
                (row["started_at"], row["ended_at"])
                for row in connection.execute(
                    """
                    SELECT started_at, ended_at
                    FROM work_sessions
                    WHERE project_id = ?
                      AND source = 'app_open_extra'
                      AND active_seconds > 0
                    ORDER BY id;
                    """,
                    (project_id,),
                ).fetchall()
            ]
            presence_days = self._presence_days(connection)
            updated: list[dict[str, Any]] = []
            for order, desired_seconds in sorted(requested.items()):
                stage = row_by_order[order]
                current_adjustment = int(float(stage.get("time_adjustment_seconds") or 0))
                base_stage = dict(stage)
                base_stage["time_adjustment_seconds"] = 0
                base_seconds = self._stage_time_seconds(base_stage, periods, supplemental_periods, presence_days)
                adjustment_seconds = int(desired_seconds) - int(base_seconds)
                delta = adjustment_seconds - current_adjustment
                connection.execute(
                    """
                    UPDATE project_stages
                    SET time_adjustment_seconds = ?, last_activity_at = ?, updated_at = ?
                    WHERE project_id = ? AND stage_order = ?;
                    """,
                    (adjustment_seconds, timestamp, timestamp, project_id, order),
                )
                if delta:
                    connection.execute(
                        """
                        INSERT INTO stage_time_records
                        (project_id, stage_order, seconds, source, note, created_at, updated_at)
                        VALUES (?, ?, ?, 'time_adjustment', ?, ?, ?);
                        """,
                        (
                            project_id,
                            order,
                            delta,
                            f"Tempo ajustado manualmente para {round(desired_seconds / 3600.0, 2)} h.",
                            timestamp,
                            timestamp,
                        ),
                    )
                updated.append(
                    {
                        "stage_order": order,
                        "seconds": desired_seconds,
                        "base_seconds": base_seconds,
                        "adjustment_seconds": adjustment_seconds,
                    }
                )
            self.add_activity(
                connection,
                project_id,
                "hora",
                "ajustar",
                f"Tempo ajustado em {len(updated)} etapa(s).",
                {"stages": updated},
            )
            self._sync_stage_timer(connection, project_id, timestamp)
        return self.get_project(project_id) or {}

    @staticmethod
    def _deleted_project_memory_value(value: Any) -> str:
        return str(value or "").strip().casefold()

    def _remember_deleted_project(self, connection: sqlite3.Connection, row: sqlite3.Row) -> None:
        code = self._deleted_project_memory_value(row["code"])
        model_path = self._deleted_project_memory_value(row["model_path"])
        model_name = self._deleted_project_memory_value(row["model_name"])
        if not any((code, model_path, model_name)):
            return
        connection.execute(
            """
            INSERT OR IGNORE INTO deleted_project_memory
            (code, model_path, model_name, created_at)
            VALUES (?, ?, ?, ?);
            """,
            (code, model_path, model_name, now_iso()),
        )

    def _clear_deleted_project_memory(
        self,
        connection: sqlite3.Connection,
        *,
        code: Any = "",
        model_path: Any = "",
        model_name: Any = "",
    ) -> None:
        values = {
            "code": self._deleted_project_memory_value(code),
            "model_path": self._deleted_project_memory_value(model_path),
            "model_name": self._deleted_project_memory_value(model_name),
        }
        filters: list[str] = []
        params: list[str] = []
        for field, value in values.items():
            if value:
                filters.append(f"{field} = ?")
                params.append(value)
        if not filters:
            return
        connection.execute(f"DELETE FROM deleted_project_memory WHERE {' OR '.join(filters)};", params)

    def _is_deleted_project_memory_match(
        self,
        connection: sqlite3.Connection,
        *,
        code: Any = "",
        model_path: Any = "",
        model_name: Any = "",
    ) -> bool:
        values = {
            "code": self._deleted_project_memory_value(code),
            "model_path": self._deleted_project_memory_value(model_path),
            "model_name": self._deleted_project_memory_value(model_name),
        }
        filters: list[str] = []
        params: list[str] = []
        for field, value in values.items():
            if value:
                filters.append(f"{field} = ?")
                params.append(value)
        if not filters:
            return False
        row = connection.execute(
            f"SELECT 1 FROM deleted_project_memory WHERE {' OR '.join(filters)} LIMIT 1;",
            params,
        ).fetchone()
        return row is not None

    @staticmethod
    def _clean_project_code(value: str) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        if text.lower().endswith(".db1"):
            text = text[:-4]
        text = text.replace("|", "-").replace("\\", "-").replace("/", "-").strip(" -")
        if text.startswith("[") and text.endswith("]") and text[1:-1].isdigit():
            return ""
        return text[:80] or ""

    def _unique_project_code(self, connection: sqlite3.Connection, base_code: str) -> str:
        code = base_code
        suffix = 2
        while connection.execute("SELECT 1 FROM projects WHERE code = ? LIMIT 1;", (code,)).fetchone():
            code = f"{base_code}-{suffix}"
            suffix += 1
        return code

    @staticmethod
    def _is_model_naming_candidate(object_type: str, properties: dict[str, Any]) -> bool:
        type_name = object_type.lower()
        ignored = ("reference", "bolt", "weld", "rebar", "reinforcement", "surface", "fitting", "cut")
        if any(item in type_name for item in ignored):
            return False
        if any(key in properties for key in ("AssemblyNumber", "PartNumber", "AssemblyPosition", "PartPosition", "Profile", "Material", "Name")):
            return True
        return any(item in type_name for item in ("beam", "plate", "part", "column", "polybeam", "contour"))

    def _has_assembly_number(self, properties: dict[str, Any]) -> bool:
        return self._has_numbered_position(properties, "Assembly")

    def _has_part_number(self, properties: dict[str, Any]) -> bool:
        return self._has_numbered_position(properties, "Part")

    def _has_numbered_position(self, properties: dict[str, Any], prefix: str) -> bool:
        for key in (f"{prefix}Position", f"{prefix}Pos", f"{prefix.upper()}_POS"):
            value = str(properties.get(key) or "").strip()
            if re.search(r"\d", value):
                return True
        number = properties.get(f"{prefix}Number")
        if not isinstance(number, dict):
            return False
        position = str(number.get("Position") or number.get("PositionString") or "").strip()
        if re.search(r"\d", position):
            return True
        number_prefix = str(number.get("Prefix") or "").strip()
        start = parse_int(number.get("StartNumber"), 0)
        return start > 0 and bool(re.search(r"\d", number_prefix))

    @staticmethod
    def _has_tag_name(text: str, properties: dict[str, Any]) -> bool:
        raw_name = str(properties.get("Name") or text or "").strip()
        if not raw_name:
            return False
        profile = properties.get("Profile")
        if isinstance(profile, dict) and raw_name.casefold() == str(profile.get("ProfileString") or "").strip().casefold():
            return False
        normalized = "".join(char for char in raw_name.upper() if char.isalnum() or char in "-_.")
        generic_names = {
            "BEAM",
            "COLUMN",
            "PLATE",
            "PART",
            "CONTOURPLATE",
            "POLYBEAM",
            "PANEL",
            "ASSEMBLY",
        }
        if normalized.replace("-", "").replace("_", "").replace(".", "") in generic_names:
            return False
        return bool(TAG_NAME_RE.match(normalized)) and any(char.isalpha() for char in normalized) and any(char.isdigit() for char in normalized)

    @staticmethod
    def _parse_iso_datetime(value: str) -> datetime | None:
        try:
            return datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _indicator_item_sort(value: Any) -> tuple[int, str]:
        text = str(value or "").strip()
        try:
            return (int(float(text)), text)
        except (TypeError, ValueError):
            return (10**9, text)

    def get_project_indicators(
        self, project_id: int, connection: sqlite3.Connection | None = None
    ) -> dict[str, Any]:
        owns_connection = connection is None
        conn = connection or self.connect()
        try:
            indicator_row = conn.execute(
                "SELECT * FROM project_indicators WHERE project_id = ?;", (project_id,)
            ).fetchone()
            rows = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM project_indicator_assemblies WHERE project_id = ?;",
                    (project_id,),
                ).fetchall()
            ]
            rows.sort(key=lambda row: self._indicator_item_sort(row.get("item_no")))
            for row in rows:
                row["item"] = str(row.get("item_no") or "")
                row["weight"] = round(float(row.get("weight") or 0), 3)
                row["unit_weight"] = round(float(row.get("unit_weight") or 0), 3)
            indicator = dict(indicator_row) if indicator_row else {
                "project_id": project_id,
                "drawing_code": "",
                "revision": "",
                "total_weight": 0.0,
                "declared_weight": 0.0,
                "page_count": 0,
                "source_type": "",
                "source_path": "",
                "source_name": "",
                "imported_at": None,
                "updated_at": None,
            }
            total_weight = round(float(indicator.get("total_weight") or 0), 3)
            declared_weight = round(float(indicator.get("declared_weight") or total_weight), 3)
            page_count = max(0, parse_int(indicator.get("page_count"), 0))
            calculated_weight = round(sum(float(row.get("weight") or 0) for row in rows), 3)
            indicator.update(
                {
                    "total_weight": total_weight,
                    "declared_weight": declared_weight,
                    "page_count": page_count,
                    "calculated_weight": calculated_weight,
                    "difference": round(calculated_weight - total_weight, 3),
                    "assembly_count": len(rows),
                    "assemblies": rows,
                }
            )
            return indicator
        finally:
            if owns_connection:
                conn.close()

    def save_project_indicators(
        self, project_id: int, data: dict[str, Any], source_type: str = "manual"
    ) -> dict[str, Any]:
        timestamp = now_iso()
        clean_source = str(source_type or data.get("source_type") or "manual").strip().lower() or "manual"
        drawing_code = str(data.get("drawing_code") or "").strip()
        revision = str(data.get("revision") or "").strip()
        total_weight = max(0.0, parse_float(data.get("total_weight"), 0.0))
        declared_weight = max(0.0, parse_float(data.get("declared_weight"), total_weight))
        page_count = max(0, parse_int(data.get("page_count"), 0))
        source_path = str(data.get("source_path") or "").strip()
        source_name = str(data.get("source_name") or "").strip()
        raw_assemblies = data.get("assemblies") or []
        assemblies: list[dict[str, Any]] = []
        for index, raw in enumerate(raw_assemblies, start=1):
            if not isinstance(raw, dict):
                continue
            item = str(raw.get("item") or raw.get("item_no") or index).strip()
            tag = str(raw.get("tag") or "").strip().upper()
            if not tag:
                continue
            assemblies.append(
                {
                    "item": item,
                    "quantity": max(1, parse_int(raw.get("quantity"), 1)),
                    "tag": tag,
                    "unit_weight": max(0.0, parse_float(raw.get("unit_weight"), raw.get("weight") or 0)),
                    "weight": max(0.0, parse_float(raw.get("weight"), 0.0)),
                    "source_type": str(raw.get("source_type") or clean_source).strip().lower() or clean_source,
                }
            )
        if not total_weight and assemblies:
            total_weight = round(sum(row["weight"] for row in assemblies), 3)
        if not declared_weight:
            declared_weight = total_weight

        with self._lock, self.session() as connection:
            project = connection.execute(
                "SELECT code, revision FROM projects WHERE id = ?;", (project_id,)
            ).fetchone()
            if project is None:
                raise ValueError("Projeto não encontrado.")
            drawing_code = drawing_code or str(project["code"] or "")
            revision = revision or str(project["revision"] or "")
            imported_at = timestamp if clean_source == "pdf" else data.get("imported_at")
            connection.execute(
                """
                INSERT INTO project_indicators
                (project_id, drawing_code, revision, total_weight, declared_weight, page_count, source_type,
                 source_path, source_name, imported_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    drawing_code = excluded.drawing_code,
                    revision = excluded.revision,
                    total_weight = excluded.total_weight,
                    declared_weight = excluded.declared_weight,
                    page_count = CASE
                        WHEN excluded.page_count > 0 THEN excluded.page_count
                        ELSE project_indicators.page_count
                    END,
                    source_type = excluded.source_type,
                    source_path = CASE
                        WHEN excluded.source_path <> '' THEN excluded.source_path
                        ELSE project_indicators.source_path
                    END,
                    source_name = CASE
                        WHEN excluded.source_name <> '' THEN excluded.source_name
                        ELSE project_indicators.source_name
                    END,
                    imported_at = CASE
                        WHEN excluded.imported_at IS NOT NULL THEN excluded.imported_at
                        ELSE project_indicators.imported_at
                    END,
                    updated_at = excluded.updated_at;
                """,
                (
                    project_id, drawing_code, revision, total_weight, declared_weight, page_count, clean_source,
                    source_path, source_name, imported_at, timestamp,
                ),
            )
            connection.execute(
                "DELETE FROM project_indicator_assemblies WHERE project_id = ?;", (project_id,)
            )
            for row in assemblies:
                connection.execute(
                    """
                    INSERT INTO project_indicator_assemblies
                    (project_id, item_no, quantity, tag, unit_weight, weight, source_type, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        project_id, row["item"], row["quantity"], row["tag"], row["unit_weight"],
                        row["weight"], row["source_type"], timestamp, timestamp,
                    ),
                )
            connection.execute(
                "UPDATE projects SET updated_at = ? WHERE id = ?;", (timestamp, project_id)
            )
            action = "importar" if clean_source == "pdf" else "editar"
            message = (
                f"Indicadores importados do PDF: {len(assemblies)} conjuntos e {total_weight:.2f} kg."
                if clean_source == "pdf"
                else f"Indicadores atualizados manualmente: {len(assemblies)} conjuntos e {total_weight:.2f} kg."
            )
            self.add_activity(
                connection, project_id, "indicadores", action, message,
                {"total_weight": total_weight, "assembly_count": len(assemblies), "page_count": page_count, "source_path": source_path},
            )
        return self.get_project_indicators(project_id)

    def activity(self, project_id: int | None = None, limit: int = 50, connection: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
        owns_connection = connection is None
        conn = connection or self.connect()
        try:
            if project_id:
                rows = conn.execute(
                    "SELECT * FROM activity_history WHERE project_id = ? ORDER BY id DESC LIMIT ?;",
                    (project_id, limit),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM activity_history ORDER BY id DESC LIMIT ?;", (limit,)).fetchall()
            return [dict(row) for row in rows]
        finally:
            if owns_connection:
                conn.close()

    def add_activity(
        self,
        connection: sqlite3.Connection,
        project_id: int | None,
        entity: str,
        action: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO activity_history (project_id, entity, action, message, details_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?);
            """,
            (project_id, entity, action, message, to_json(details or {}), now_iso()),
        )

    def heartbeat_runtime(self) -> None:
        """Atualiza o relógio complementar enquanto o processo do programa está aberto."""
        with self._lock, self.session() as connection:
            self._record_presence(connection)
            self._heartbeat_runtime_session(connection)

    def dashboard(self) -> dict[str, Any]:
        # O heartbeat grava no SQLite. Essa gravação precisa ser confirmada antes
        # de list_projects() sincronizar o Progresso de Detalhamento em outra
        # conexão; manter as duas operações dentro da mesma sessão externa fazia
        # a atualização periódica falhar com "database is locked". Como o front-end
        # trata a consulta automática como silenciosa, a tela permanecia no último
        # percentual válido (por exemplo, 66%).
        with self._lock:
            with self.session() as connection:
                self._record_presence(connection)
                self._heartbeat_runtime_session(connection)

            # list_projects() lê o JSON atualizado e grava o percentual real da
            # etapa Detalhamento. Aqui a transação anterior já foi concluída.
            projects = self.list_projects()
            active_summary = next(
                (project for project in projects if int(project.get("active") or 0)),
                None,
            )
            active = self.get_project(int(active_summary["id"])) if active_summary else None
            completed = [
                project
                for project in projects
                if project["status"] == "Concluído" or float(project.get("progress") or 0) >= 100
            ][:8]

            with self.session() as connection:
                return {
                    "active_project": active,
                    "projects": projects,
                    "completed_projects": completed,
                    "activity": self.activity(None, 80, connection),
                    "settings": {
                        "user_name": self.setting("user_name", "CaLavort"),
                        "user_nickname": self.setting("user_nickname", "CaLavort"),
                        "projectist_name": self.setting("projectist_name", ""),
                        "projectist_abbreviation": self.setting("projectist_abbreviation", "JEC"),
                        "report_output_format": self.setting("report_output_format", "html"),
                        "auto_description_enabled": self.setting("auto_description_enabled", "1"),
                        "last_model_path": self.setting("last_model_path", ""),
                        "last_model_parent": self.setting("last_model_parent", ""),
                        "last_document_folder": self.setting("last_document_folder", ""),
                        "last_document_parent": self.setting("last_document_parent", ""),
                        "last_spreadsheet_path": self.setting("last_spreadsheet_path", ""),
                        "last_spreadsheet_parent": self.setting("last_spreadsheet_parent", ""),
                        "project_statuses": from_json(
                            self.setting(
                                "project_statuses",
                                to_json(["Em andamento", "Planejamento", "Pausado", "Concluído"]),
                            ),
                            ["Em andamento", "Planejamento", "Pausado", "Concluído"],
                        ),
                        "project_form_suggestions": from_json(
                            self.setting(
                                "project_form_suggestions",
                                to_json({
                                    "os_number": [], "client": [], "area": [],
                                    "checker_name": [], "approver_name": []
                                }),
                            ),
                            {
                                "os_number": [], "client": [], "area": [],
                                "checker_name": [], "approver_name": []
                            },
                        ),
                    },
                    "weight_profile": self.get_weight_profile(),
                }

    def recalibration_suggestion(self, project_id: int) -> dict[str, Any]:
        with self._lock, self.session() as connection:
            rows = connection.execute(
                """
                SELECT stage_order, COALESCE(SUM(seconds), 0) AS seconds
                FROM stage_time_records
                WHERE project_id = ?
                GROUP BY stage_order;
                """,
                (project_id,),
            ).fetchall()
            seconds_by_stage = {str(row["stage_order"]): float(row["seconds"] or 0) for row in rows}
            profile = self.get_weight_profile()
            current = normalize_weights({str(key): parse_float(value) for key, value in profile["weights"].items()})
            observed = observed_weights_from_seconds(seconds_by_stage)
            suggested = smooth_weights(current, observed, parse_float(profile.get("smoothing_factor"), 0.30))
            return {
                "project_id": project_id,
                "seconds_by_stage": seconds_by_stage,
                "current": current,
                "observed": observed,
                "suggested": suggested,
            }
