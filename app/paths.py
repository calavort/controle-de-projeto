from __future__ import annotations

from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]
APP_DIR = BASE_DIR / "app"
DATA_DIR = BASE_DIR / "data"
BACKUP_DIR = DATA_DIR / "backups"
EXPORT_DIR = BASE_DIR / "exports"
LOG_DIR = BASE_DIR / "logs"
DB_PATH = DATA_DIR / "controle_projetos.sqlite"
CONFIG_PATH = BASE_DIR / "config.json"
TEKLA_ROOT_FILE = BASE_DIR / "tekla-root.txt"


def ensure_folders() -> None:
    for folder in (DATA_DIR, BACKUP_DIR, EXPORT_DIR, LOG_DIR):
        folder.mkdir(parents=True, exist_ok=True)

