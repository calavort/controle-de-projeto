# -*- coding: utf-8 -*-
"""
Progresso de detalhamento
Programa desenvolvido por Edflávio Calavort - 2026

Objetivo:
- Identificar os conjuntos esperados no modelo Tekla.
- Considerar um item iniciado somente quando o TEXTO VISÍVEL de uma marca contiver um subitem decimal, como 60.1.
- Ignorar balões inteiros, como 10, 25 ou 33, mesmo quando estejam associados a peças 10.1, 25.1 ou 33.1.
- Percorrer as vistas do multidesenho, ignorando vistas do tipo DetailView como evidência de início.
- Exibir os itens principais, TAGs e folhas temporárias correspondentes.
- Permitir concluir manualmente o registro de tempo do grupo em andamento.

O programa reutiliza apenas a estratégia essencial de conexão com a Tekla Open API e a
leitura mínima de TAG/numeração dos programas de referência fornecidos pelo usuário.
"""

from __future__ import annotations

import ctypes
import hashlib
import math
import json
import os
import re
import subprocess
import sys
import time
import traceback
import uuid
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

# Consciência de DPI antes de iniciar o WebView.
if sys.platform.startswith("win"):
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

try:
    import webview
except Exception:
    webview = None

APP_NAME = "Progresso de detalhamento"
BASE_DIR = Path(__file__).resolve().parent
HTML_FILE = BASE_DIR / "interface.html"
CONFIG_FILE = BASE_DIR / "config.json"
HISTORY_FILE = BASE_DIR / "estatisticas_detalhamento.json"
TEKLA_ROOT_FILE = BASE_DIR / "tekla-root.txt"
IS_WINDOWS = sys.platform.startswith("win")

HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010

APP_WIDTH = 330
APP_HEIGHT = 560
TEMP_PAGE_WIDTH = 841.0
TEMP_PAGE_HEIGHT = 594.0

DIMENSION_TYPE_HINTS = (
    "Dimension",
    "DimensionSet",
    "StraightDimension",
    "CurvedDimension",
    "RadialDimension",
    "RadiusDimension",
    "AngleDimension",
)

DEFAULT_CONFIG: Dict[str, Any] = {
    "uda_principal": "FRMW",
    "uda_reserva": "AwevaFRMW",
    "manter_a_frente": True,
    "tekla_root": "",
    "intervalo_analise_segundos": 15,
    "modo_progresso": "conjunto",
    # No modo por componente, marcas existentes nessas vistas-base servem apenas
    # para localizar as pecas; elas nao comprovam que o componente foi detalhado.
    "vistas_base_ignoradas": ["VISTA FRONTAL"],
}

COMMON_TAG_PROPERTIES = (
    "FRMW",
    "AwevaFRMW",
    "AwezaFRMW",
    "AWEVAFRMW",
    "AWEZAFRMW",
    "FBMW",
    "TAG",
)




def get_window_title(hwnd: int) -> str:
    if not IS_WINDOWS:
        return ""
    user32 = ctypes.windll.user32
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def find_app_window() -> int:
    if not IS_WINDOWS:
        return 0
    user32 = ctypes.windll.user32
    found: List[int] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd, _lparam):
        try:
            if user32.IsWindowVisible(hwnd):
                title = get_window_title(hwnd).strip()
                if APP_NAME.casefold() in title.casefold():
                    found.append(int(hwnd))
        except Exception:
            pass
        return True

    user32.EnumWindows(callback_type(callback), 0)
    return found[0] if found else 0


def set_native_topmost(enabled: bool) -> None:
    if not IS_WINDOWS:
        return
    hwnd = find_app_window()
    if not hwnd:
        return
    ctypes.windll.user32.SetWindowPos(
        wintypes.HWND(hwnd),
        wintypes.HWND(HWND_TOPMOST if enabled else HWND_NOTOPMOST),
        0, 0, 0, 0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
    )


def now_time() -> str:
    return datetime.now().strftime("%H:%M:%S")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def parse_iso(value: Any) -> Optional[datetime]:
    text = clean_text(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except Exception:
        return None


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    try:
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, (int, float, bool)):
            return str(value).strip()
        try:
            type_name = str(value.GetType().FullName)
            if "System.Collections" in type_name:
                return ""
        except Exception:
            pass
        return str(value).strip()
    except Exception:
        return ""


def iter_net(collection: Any) -> Iterable[Any]:
    if collection is None:
        return
    try:
        enum = collection.GetEnumerator()
        while enum.MoveNext():
            yield enum.Current
        return
    except Exception:
        pass
    try:
        for item in collection:
            yield item
    except Exception:
        return


def object_type_name(obj: Any) -> str:
    if obj is None:
        return ""
    try:
        return str(obj.GetType().Name)
    except Exception:
        try:
            return type(obj).__name__
        except Exception:
            return ""


def object_identifier_key(obj: Any) -> str:
    if obj is None:
        return ""
    try:
        return f"ID={int(obj.Identifier.ID)}"
    except Exception:
        try:
            return f"GUID={obj.Identifier.GUID}"
        except Exception:
            return f"OBJ={id(obj)}"


def parse_item_number(value: Any) -> Optional[int]:
    """Extrai o item principal usado na lista de material. Ex.: 50. ou 50 -> 50."""
    text = clean_text(value).replace(",", ".")
    if not text:
        return None
    match = re.search(r"(?<!\d)(\d+)(?:\.\d+)?", text)
    if not match:
        return None
    try:
        number = int(match.group(1))
        return number if number > 0 else None
    except Exception:
        return None


def parse_subitem_position(value: Any) -> Optional[Tuple[int, int]]:
    """Aceita somente posição decimal real, como 60.1 ou 60,1.

    Números inteiros (10, 25, 33), escalas e textos sem subitem não indicam
    que o detalhamento do conjunto foi iniciado.
    """
    text = clean_text(value)
    if not text:
        return None
    match = re.search(r"(?<!\d)(\d+)\s*[.,]\s*(\d+)(?!\d)", text)
    if not match:
        return None
    try:
        parent = int(match.group(1))
        child = int(match.group(2))
    except Exception:
        return None
    if parent <= 0 or child <= 0:
        return None
    return parent, child


def numeric_sort(values: Iterable[int]) -> List[int]:
    return sorted({int(value) for value in values if int(value) > 0})


def normalize_tag(value: Any) -> str:
    text = clean_text(value)
    if not text:
        return ""
    return re.sub(r"\s+", "", text).upper()


def extract_project_number(*values: Any) -> str:
    """Localiza o número do desenho/projeto, priorizando o padrão IME."""
    texts = [clean_text(value).upper() for value in values if clean_text(value)]
    patterns = (
        r"\bIME-[A-Z0-9]+(?:-[A-Z0-9]+){2,}\b",
        r"\b[A-Z]{2,}(?:-[A-Z0-9]+){3,}\b",
    )
    for pattern in patterns:
        for text in texts:
            match = re.search(pattern, text)
            if match:
                return match.group(0).strip("- ")
    return ""


def project_identity_key(
    project_number: Any, drawing_name: Any, model_name: Any, model_path: Any = ""
) -> str:
    # O número do projeto é a identidade principal. Nome do desenho, revisão e
    # nome do modelo podem mudar durante o trabalho e não devem criar outro
    # histórico para o mesmo projeto.
    number = clean_text(project_number)
    if number:
        raw_key = f"project:{number.casefold()}"
    else:
        stable_parts = [clean_text(drawing_name), clean_text(model_name)]
        if any(stable_parts):
            raw_key = "|".join(part.casefold() for part in stable_parts if part)
        else:
            raw_key = clean_text(model_path).casefold() or "progresso-de-detalhamento"
    return hashlib.sha1(raw_key.encode("utf-8", errors="ignore")).hexdigest()


def looks_like_support_tag(value: Any) -> bool:
    text = normalize_tag(value)
    if not text or len(text) < 8:
        return False
    if text.startswith(("IME-", "DWG-", "DOC-", "REV-")):
        return False
    if text.startswith(("ISUP-", "ESUP-", "PSUP-", "STEL-")):
        return True
    if "SUP" in text and text.count("-") >= 2:
        return True
    return text.count("-") >= 3 and bool(re.search(r"[A-Z]", text))


def load_config() -> Dict[str, Any]:
    data = dict(DEFAULT_CONFIG)
    try:
        if CONFIG_FILE.exists():
            loaded = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data.update(loaded)
    except Exception:
        pass
    return data


def save_config(config: Dict[str, Any]) -> None:
    clean = dict(DEFAULT_CONFIG)
    clean.update(config or {})
    CONFIG_FILE.write_text(
        json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8"
    )


class TeklaBridge:
    """Conexão e leitura mínima da Tekla Open API."""

    def __init__(self, logger):
        self.logger = logger
        self.loaded = False
        self.bin_dir: Optional[Path] = None
        self.model = None
        self.drawing_handler = None
        self.Model = None
        self.ModelObject = None
        self.Assembly = None
        self.DrawingHandler = None
        self.DrawingMark = None
        self.DrawingMarkSet = None
        self.DrawingPart = None
        self.View = None
        self._dll_handles: List[Any] = []
        self._assembly_resolver = None
        self._expected_cache: Dict[str, Tuple[Dict[int, str], Dict[str, Any]]] = {}
        self._expected_subitems_cache: Dict[str, Tuple[Dict[Tuple[int, int], str], Dict[str, Any]]] = {}
        self._temporary_pages_cache: Dict[str, List[Dict[str, Any]]] = {}

    def _log(self, message: Any) -> None:
        self.logger(clean_text(message))

    @staticmethod
    def _run_hidden(command: List[str]) -> str:
        flags = 0
        if IS_WINDOWS and hasattr(subprocess, "CREATE_NO_WINDOW"):
            flags = subprocess.CREATE_NO_WINDOW
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=flags,
            )
            if result.returncode == 0:
                return (result.stdout or "").strip()
        except Exception:
            pass
        return ""

    def running_tekla_executables(self) -> List[Path]:
        """Obtém o executável da instância realmente aberta, evitando carregar outra versão."""
        if not IS_WINDOWS:
            return []
        commands = [
            [
                "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                "$p=Get-CimInstance Win32_Process -Filter \"Name='TeklaStructures.exe'\" | "
                "Select-Object -First 1; if($p){$p.ExecutablePath}",
            ],
            [
                "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                "$p=Get-Process TeklaStructures -ErrorAction SilentlyContinue | "
                "Select-Object -First 1; if($p){$p.Path}",
            ],
            ["wmic", "process", "where", "name='TeklaStructures.exe'", "get", "ExecutablePath", "/value"],
        ]
        found: List[Path] = []
        seen: Set[str] = set()
        for command in commands:
            output = self._run_hidden(command)
            for line in output.splitlines():
                line = line.strip()
                if not line:
                    continue
                if line.lower().startswith("executablepath="):
                    line = line.split("=", 1)[1].strip()
                if not line.lower().endswith("teklastructures.exe"):
                    continue
                key = line.casefold()
                if key not in seen:
                    seen.add(key)
                    found.append(Path(line))
        return found

    @staticmethod
    def dll_search_dirs(bin_dir: Path) -> List[Path]:
        candidates = [
            bin_dir,
            bin_dir / "Net48Runtime",
            bin_dir / "net48",
            bin_dir / "nt" / "bin",
            bin_dir.parent / "bin",
            bin_dir.parent / "bin" / "Net48Runtime",
        ]
        result: List[Path] = []
        seen: Set[str] = set()
        for candidate in candidates:
            key = str(candidate).casefold()
            if key not in seen and candidate.exists() and candidate.is_dir():
                seen.add(key)
                result.append(candidate)

        # Mesma proteção usada no Nomeador: dependências Tekla/Trimble podem estar em subpastas.
        try:
            for dll in bin_dir.rglob("*.dll"):
                name = dll.name.casefold()
                if name.startswith("tekla.") or name.startswith("trimble."):
                    folder = dll.parent
                    key = str(folder).casefold()
                    if key not in seen:
                        seen.add(key)
                        result.append(folder)
        except Exception:
            pass
        return result

    def find_dll(self, bin_dir: Path, dll_name: str) -> Optional[Path]:
        for folder in self.dll_search_dirs(bin_dir):
            candidate = folder / dll_name
            if candidate.exists():
                return candidate
        try:
            return next(bin_dir.rglob(dll_name), None)
        except Exception:
            return None

    def is_valid_bin(self, path: Path) -> bool:
        path = Path(path)
        return (
            path.exists()
            and self.find_dll(path, "Tekla.Structures.dll") is not None
            and self.find_dll(path, "Tekla.Structures.Model.dll") is not None
            and self.find_dll(path, "Tekla.Structures.Drawing.dll") is not None
        )

    @staticmethod
    def _bin_candidates_from_executable(exe_path: Path) -> Iterable[Path]:
        exe_path = Path(exe_path)
        bases = [exe_path.parent]
        bases.extend(list(exe_path.parents)[:5])
        seen: Set[str] = set()
        for base in bases:
            for candidate in (base, base / "bin", base / "nt" / "bin"):
                key = str(candidate).casefold()
                if key not in seen:
                    seen.add(key)
                    yield candidate

    @staticmethod
    def _read_manual_roots(configured_root: str = "") -> List[Path]:
        roots: List[Path] = []
        if configured_root:
            roots.append(Path(configured_root.strip().strip('"')))
        if TEKLA_ROOT_FILE.exists():
            try:
                for line in TEKLA_ROOT_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
                    line = line.strip().strip('"')
                    if line and not line.startswith("#"):
                        roots.append(Path(line))
            except Exception:
                pass
        return roots

    @staticmethod
    def _standard_roots() -> List[Path]:
        roots: List[Path] = []
        for env_name in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
            value = os.environ.get(env_name)
            if value:
                base = Path(value)
                roots.extend([base / "Tekla Structures", base / "Trimble" / "Tekla Structures"])
        roots.extend([
            Path(r"C:\TeklaStructures"), Path(r"C:\Tekla Structures"),
            Path(r"D:\TeklaStructures"), Path(r"D:\Tekla Structures"),
        ])
        return roots

    def find_bin(self, configured_root: str = "") -> Path:
        # 1. A versão da instância aberta tem prioridade absoluta.
        for executable in self.running_tekla_executables():
            self._log(f"Tekla aberto detectado: {executable}")
            for candidate in self._bin_candidates_from_executable(executable):
                if self.is_valid_bin(candidate):
                    return candidate

        # 2. Caminho informado nas configurações ou no tekla-root.txt.
        for root in self._read_manual_roots(configured_root):
            for candidate in (root, root / "bin", root / "nt" / "bin"):
                if self.is_valid_bin(candidate):
                    return candidate

        # 3. Busca automática nas instalações conhecidas, priorizando versões mais novas.
        installed: List[Path] = []
        seen: Set[str] = set()
        for root in self._standard_roots():
            candidates: List[Path] = []
            if root.name.casefold() == "bin":
                candidates.append(root)
            if root.exists():
                candidates.extend([root / "bin", root / "nt" / "bin"])
                try:
                    folders = sorted(
                        [item for item in root.iterdir() if item.is_dir()],
                        key=lambda item: item.name,
                        reverse=True,
                    )
                    for folder in folders:
                        candidates.extend([folder / "bin", folder / "nt" / "bin"])
                except Exception:
                    pass
            for candidate in candidates:
                key = str(candidate).casefold()
                if key not in seen and self.is_valid_bin(candidate):
                    seen.add(key)
                    installed.append(candidate)
        if installed:
            return installed[0]

        raise RuntimeError(
            "Não foi possível localizar a instalação do Tekla. "
            "Mantenha o Tekla aberto ou informe a pasta da versão nas configurações."
        )

    def _activate_assembly_resolver(self, folders: List[Path]) -> None:
        try:
            import clr  # noqa: F401
            from System import AppDomain
            from System.Reflection import Assembly, AssemblyName

            def resolve(_sender, args):
                try:
                    dll_name = AssemblyName(args.Name).Name + ".dll"
                    for folder in folders:
                        candidate = folder / dll_name
                        if candidate.exists():
                            return Assembly.LoadFrom(str(candidate))
                except Exception:
                    return None
                return None

            self._assembly_resolver = resolve
            AppDomain.CurrentDomain.AssemblyResolve += self._assembly_resolver
        except Exception as exc:
            self._log(f"Aviso ao ativar AssemblyResolve: {exc}")

    def prepare_environment(self, configured_root: str = "") -> None:
        chosen = self.find_bin(configured_root)
        self.bin_dir = chosen
        folders = self.dll_search_dirs(chosen)
        prepend: List[str] = []
        current_path = os.environ.get("PATH", "")
        for folder in folders:
            folder_text = str(folder)
            if folder_text not in sys.path:
                sys.path.insert(0, folder_text)
            if folder_text.casefold() not in current_path.casefold():
                prepend.append(folder_text)
            try:
                if hasattr(os, "add_dll_directory"):
                    self._dll_handles.append(os.add_dll_directory(folder_text))
            except Exception:
                pass
        if prepend:
            os.environ["PATH"] = os.pathsep.join(prepend) + os.pathsep + current_path
        self._activate_assembly_resolver(folders)
        self._log(f"API Tekla: {chosen}")

    def load_api(self, configured_root: str = "") -> None:
        if self.loaded:
            return
        self.prepare_environment(configured_root)
        try:
            import clr

            for dll_name in (
                "Tekla.Structures.dll",
                "Tekla.Structures.Model.dll",
                "Tekla.Structures.Drawing.dll",
            ):
                dll_path = self.find_dll(self.bin_dir, dll_name) if self.bin_dir else None
                if dll_path is None:
                    raise RuntimeError(f"DLL obrigatória não encontrada: {dll_name}")
                clr.AddReference(str(dll_path))

            from Tekla.Structures.Model import Model, ModelObject, Assembly
            from Tekla.Structures.Drawing import DrawingHandler, Mark, MarkSet, Part as DrawingPart, View

            self.Model = Model
            self.ModelObject = ModelObject
            self.Assembly = Assembly
            self.DrawingHandler = DrawingHandler
            self.DrawingMark = Mark
            self.DrawingMarkSet = MarkSet
            self.DrawingPart = DrawingPart
            self.View = View
            self.loaded = True
        except Exception as exc:
            self._log(traceback.format_exc())
            raise RuntimeError(f"Falha ao carregar a API do Tekla: {exc}") from exc

    def connect(self, configured_root: str = "", retries: int = 5, delay: float = 0.6) -> Tuple[bool, bool]:
        self.load_api(configured_root)
        model_ok = False
        drawing_ok = False
        for attempt in range(1, retries + 1):
            try:
                self.model = self.Model()
                model_ok = bool(self.model.GetConnectionStatus())
            except Exception:
                model_ok = False
            try:
                self.drawing_handler = self.DrawingHandler()
                drawing_ok = bool(self.drawing_handler.GetConnectionStatus())
            except Exception:
                drawing_ok = False
            self._log(
                f"Conexão Tekla {attempt}/{retries}: modelo={model_ok}, desenho={drawing_ok}"
            )
            if model_ok and drawing_ok:
                return True, True
            if attempt < retries:
                time.sleep(delay)
        return model_ok, drawing_ok

    def connection_state(self, configured_root: str = "") -> Dict[str, Any]:
        try:
            model_ok, drawing_ok = self.connect(configured_root, retries=2, delay=0.3)
            active = self.drawing_handler.GetActiveDrawing() if drawing_ok else None
            drawing_name = ""
            if active is not None:
                drawing_name = clean_text(getattr(active, "Name", "")) or clean_text(
                    getattr(active, "Mark", "")
                )
            return {
                "ok": model_ok and drawing_ok and active is not None,
                "model": model_ok,
                "drawing": drawing_ok,
                "active_drawing": active is not None,
                "drawing_name": drawing_name,
                "message": (
                    "Tekla conectado"
                    if model_ok and drawing_ok and active is not None
                    else "Abra o multidesenho no Tekla"
                    if model_ok and drawing_ok
                    else "Tekla não conectado"
                ),
            }
        except Exception as exc:
            return {
                "ok": False,
                "model": False,
                "drawing": False,
                "active_drawing": False,
                "drawing_name": "",
                "message": clean_text(exc),
            }

    def require_active_drawing(self, configured_root: str = ""):
        model_ok, drawing_ok = self.connect(configured_root, retries=5, delay=0.7)
        if not model_ok:
            raise RuntimeError(
                "Não foi possível conectar ao modelo do Tekla. "
                "Mantenha o Tekla aberto com o modelo carregado e execute ambos no mesmo nível de privilégio."
            )
        if not drawing_ok:
            raise RuntimeError("Não foi possível conectar ao editor de desenhos do Tekla.")
        drawing = self.drawing_handler.GetActiveDrawing()
        if drawing is None:
            raise RuntimeError("Abra o multidesenho que será analisado.")
        return drawing

    def model_identity(self, drawing: Any) -> Dict[str, str]:
        model_path = ""
        model_name = ""
        try:
            info = self.model.GetInfo()
        except Exception:
            info = None
        if info is not None:
            for attr_name in ("ModelPath", "Path"):
                try:
                    model_path = clean_text(getattr(info, attr_name))
                except Exception:
                    model_path = ""
                if model_path:
                    break
            for attr_name in ("ModelName", "Name"):
                try:
                    model_name = clean_text(getattr(info, attr_name))
                except Exception:
                    model_name = ""
                if model_name:
                    break

        drawing_values: List[str] = []
        for attr_name in ("Name", "Mark", "Title1", "Title2", "Title3"):
            try:
                value = clean_text(getattr(drawing, attr_name))
            except Exception:
                value = ""
            if value:
                drawing_values.append(value)

        drawing_name = drawing_values[0] if drawing_values else ""
        project_number = extract_project_number(
            *drawing_values, model_name, Path(model_path).name if model_path else "", model_path
        )
        project_key = project_identity_key(
            project_number, drawing_name, model_name, model_path
        )
        # Assinatura estável da FOLHA (desenho) ativa, usada para somar o
        # progresso por folha sem depender de um único título repetido entre
        # folhas. Usa todos os rótulos do desenho + caminho do modelo.
        signature_source = "|".join([*drawing_values, model_path])
        drawing_signature = (
            hashlib.sha1(signature_source.casefold().encode("utf-8", errors="ignore")).hexdigest()
            if signature_source
            else ""
        )
        return {
            "project_key": project_key,
            "project_number": project_number,
            "model_path": model_path,
            "model_name": model_name,
            "drawing_name": drawing_name,
            "drawing_signature": drawing_signature,
        }

    @staticmethod
    def get_user_property(obj: Any, prop_name: str) -> str:
        if obj is None or not prop_name:
            return ""
        try:
            result = obj.GetUserProperty(str(prop_name), "")
            if isinstance(result, tuple) and len(result) >= 2 and result[0]:
                return clean_text(result[1])
        except Exception:
            pass
        try:
            import clr
            from System import String

            ref_value = clr.Reference[String]("")
            if obj.GetUserProperty(str(prop_name), ref_value):
                return clean_text(ref_value.Value)
        except Exception:
            pass
        return ""

    @staticmethod
    def get_report_property(obj: Any, prop_name: str) -> str:
        if obj is None or not prop_name:
            return ""
        try:
            result = obj.GetReportProperty(str(prop_name), "")
            if isinstance(result, tuple) and len(result) >= 2 and result[0]:
                return clean_text(result[1])
        except Exception:
            pass
        try:
            import clr
            from System import String

            ref_value = clr.Reference[String]("")
            if obj.GetReportProperty(str(prop_name), ref_value):
                return clean_text(ref_value.Value)
        except Exception:
            pass
        return ""

    @staticmethod
    def get_report_value(obj: Any, prop_name: str) -> Any:
        """Lê propriedade de relatório como texto, inteiro ou número real."""
        text = TeklaBridge.get_report_property(obj, prop_name)
        if text:
            return text
        if obj is None or not prop_name:
            return ""
        try:
            import clr
            from System import Double, Int32
            for net_type, initial in ((Double, 0.0), (Int32, 0)):
                try:
                    ref_value = clr.Reference[net_type](initial)
                    if obj.GetReportProperty(str(prop_name), ref_value):
                        return ref_value.Value
                except Exception:
                    continue
        except Exception:
            pass
        return ""

    @staticmethod
    def _dimension_text(value: Any) -> str:
        try:
            number = float(str(value).strip().replace(",", "."))
        except Exception:
            return ""
        if not math.isfinite(number) or number <= 0:
            return ""
        rounded = round(number)
        if abs(number - rounded) < 0.05:
            return str(int(rounded))
        return f"{number:.1f}".rstrip("0").rstrip(".").replace(".", ",")

    def parent_description_from_model_object(self, model_obj: Any) -> str:
        """Lê a descrição/nome do conjunto ao qual o componente pertence."""
        assembly = self.resolve_assembly(model_obj)
        candidates: List[Any] = []
        if assembly is not None:
            candidates.append(assembly)
            try:
                main = assembly.GetMainPart()
                if main is not None:
                    candidates.append(main)
            except Exception:
                pass
        if model_obj is not None:
            candidates.append(model_obj)

        for obj in candidates:
            for prop_name in (
                "ASSEMBLY_NAME",
                "ASSEMBLY NAME",
                "ASSEMBLY_DESCRIPTION",
                "ASSEMBLY DESCRIPTION",
            ):
                value = clean_text(self.get_report_value(obj, prop_name))
                if value:
                    return value
        for obj in candidates:
            try:
                value = clean_text(getattr(obj, "Name", ""))
            except Exception:
                value = ""
            if value:
                return value
        return ""

    def subitem_description_from_model_object(self, model_obj: Any) -> str:
        """Monta a descrição curta usada na LM para a peça/subitem.

        Prioriza uma descrição pronta exposta pelo modelo. Para chapas, usa
        PROFILE + WIDTH + LENGTH e produz o padrão CH. espessuraXlarguraXcomprimento.
        """
        if model_obj is None:
            return ""

        for prop_name in (
            "DESCRIPTION",
            "PART_DESCRIPTION",
            "PART DESCRIPTION",
            "PART_DESC",
            "DRAWING_DESCRIPTION",
        ):
            value = clean_text(self.get_report_value(model_obj, prop_name))
            if value and not parse_subitem_position(value):
                return value

        profile = clean_text(self.get_report_value(model_obj, "PROFILE"))
        if not profile:
            try:
                profile = clean_text(getattr(getattr(model_obj, "Profile", None), "ProfileString", ""))
            except Exception:
                profile = ""

        length = self._dimension_text(self.get_report_value(model_obj, "LENGTH"))
        width = self._dimension_text(self.get_report_value(model_obj, "WIDTH"))

        normalized_profile = profile.upper().replace(" ", "").replace(",", ".")
        plate_match = re.search(
            r"(?:PL|PLATE|CH)[-_.]?(\d+(?:\.\d+)?)(?:[*X](\d+(?:\.\d+)?))?",
            normalized_profile,
        )
        if plate_match:
            thickness = self._dimension_text(plate_match.group(1))
            profile_width = self._dimension_text(plate_match.group(2)) if plate_match.group(2) else ""
            plate_width = profile_width or width
            dims = [value for value in (thickness, plate_width, length) if value]
            if len(dims) >= 2:
                return "CH. " + "X".join(dims)

        if profile:
            if length and length not in profile:
                return f"{profile} X {length}"
            return profile

        for prop_name in ("NAME", "PART_NAME"):
            value = clean_text(self.get_report_value(model_obj, prop_name))
            if value:
                return value
        return ""

    def iter_model_objects(self) -> Iterable[Any]:
        selector = self.model.GetModelObjectSelector()
        enum = selector.GetAllObjects()
        seen: Set[str] = set()
        while enum.MoveNext():
            obj = enum.Current
            key = object_identifier_key(obj)
            if key and key not in seen:
                seen.add(key)
                yield obj

    def resolve_assembly(self, obj: Any):
        if obj is None:
            return None
        try:
            if self.Assembly is not None and isinstance(obj, self.Assembly):
                return obj
        except Exception:
            pass
        try:
            return obj.GetAssembly()
        except Exception:
            return None

    @staticmethod
    def assembly_parts(assembly: Any) -> List[Any]:
        parts: List[Any] = []
        if assembly is None:
            return parts
        try:
            main = assembly.GetMainPart()
            if main is not None:
                parts.append(main)
        except Exception:
            pass
        try:
            for part in iter_net(assembly.GetSecondaries()):
                if part is not None:
                    parts.append(part)
        except Exception:
            pass
        return parts

    def assembly_candidates(self, assembly: Any) -> List[Any]:
        candidates: List[Any] = []
        seen: Set[str] = set()

        def add(obj: Any) -> None:
            if obj is None:
                return
            key = object_identifier_key(obj)
            if key not in seen:
                seen.add(key)
                candidates.append(obj)

        try:
            add(assembly.GetMainPart())
        except Exception:
            pass
        add(assembly)
        for part in self.assembly_parts(assembly):
            add(part)
        return candidates

    @staticmethod
    def numbering_series_values(series: Any) -> Tuple[str, int]:
        if series is None:
            return "", 0
        prefix = clean_text(getattr(series, "Prefix", ""))
        try:
            start = int(getattr(series, "StartNumber", 0) or 0)
        except Exception:
            start = 0
        return prefix, start

    def item_number_for_assembly(self, assembly: Any) -> Optional[int]:
        candidates = self.assembly_candidates(assembly)

        # O Nomeador grava o item principal no prefixo da numeração da peça principal.
        for obj in candidates:
            for attr_name in ("PartNumber", "AssemblyNumber"):
                try:
                    prefix, start = self.numbering_series_values(getattr(obj, attr_name))
                except Exception:
                    continue
                value = parse_item_number(prefix)
                if value:
                    return value
                if start > 1:
                    return start

        for obj in candidates:
            for prop_name in (
                "PART_POS",
                "ASSEMBLY_POS",
                "PART POSITION",
                "ASSEMBLY POSITION",
                "PART_PREFIX",
                "ASSEMBLY_PREFIX",
            ):
                value = parse_item_number(self.get_report_property(obj, prop_name))
                if value:
                    return value
        return None

    def support_tag_for_assembly(
        self, assembly: Any, primary_uda: str, fallback_uda: str
    ) -> str:
        properties: List[str] = []
        for name in (primary_uda, fallback_uda, *COMMON_TAG_PROPERTIES):
            name = clean_text(name)
            if name and name.casefold() not in {p.casefold() for p in properties}:
                properties.append(name)

        candidates = self.assembly_candidates(assembly)
        for obj in candidates:
            for prop in properties:
                for value in (
                    self.get_user_property(obj, prop),
                    self.get_report_property(obj, prop),
                    self.get_report_property(obj, "USERDEFINED." + prop),
                ):
                    if looks_like_support_tag(value):
                        return normalize_tag(value)

        # Reserva necessária para modelos já processados pelo Nomeador.
        for obj in candidates:
            try:
                value = clean_text(getattr(obj, "Name", ""))
            except Exception:
                value = ""
            if looks_like_support_tag(value):
                return normalize_tag(value)
        return ""

    def expected_items(
        self, primary_uda: str, fallback_uda: str
    ) -> Tuple[Dict[int, str], Dict[str, int]]:
        start = time.perf_counter()
        assemblies: Dict[str, Any] = {}
        scanned = 0
        source = "all_objects"
        assemblies_enum = None
        try:
            selector = self.model.GetModelObjectSelector()
            assembly_type = self.ModelObject.ModelObjectEnum.ASSEMBLY
            assemblies_enum = selector.GetAllObjectsWithType(assembly_type)
        except Exception:
            assemblies_enum = None

        if assemblies_enum is not None:
            source = "assemblies"
            try:
                while assemblies_enum.MoveNext():
                    assembly = assemblies_enum.Current
                    if assembly is None:
                        continue
                    scanned += 1
                    key = object_identifier_key(assembly) or f"OBJ={id(assembly)}"
                    assemblies.setdefault(key, assembly)
            except Exception:
                assemblies.clear()
                scanned = 0
                source = "all_objects"

        if not assemblies:
            for obj in self.iter_model_objects():
                scanned += 1
                assembly = self.resolve_assembly(obj)
                if assembly is None:
                    continue
                key = object_identifier_key(assembly) or f"OBJ={id(assembly)}"
                assemblies.setdefault(key, assembly)
        scan_seconds = time.perf_counter() - start

        properties_start = time.perf_counter()
        items: Dict[int, str] = {}
        duplicate_numbers = 0
        no_tag = 0
        no_number = 0
        for assembly in assemblies.values():
            # O numero da lista de material e a evidencia principal de que o
            # conjunto deve ser contado. Objetos sem numeracao (chapas/pecas
            # soltas do modelo) continuam descartados aqui pelo filtro de numero.
            # A TAG de suporte (FRMW/AVEVA) segue sendo lida e usada como rotulo
            # quando existir, mas NAO e mais obrigatoria: modelos que nao usam a
            # UDA FRMW (ex.: grades de piso) tambem precisam ter seus conjuntos
            # numerados contados no total.
            number = self.item_number_for_assembly(assembly)
            if not number:
                no_number += 1
                continue
            tag = self.support_tag_for_assembly(assembly, primary_uda, fallback_uda)
            if not tag:
                no_tag += 1
            if number not in items:
                items[number] = tag
            elif tag:
                existing = items[number]
                if not existing:
                    items[number] = tag          # completa a TAG que faltava
                elif existing != tag:
                    duplicate_numbers += 1        # duas TAGs distintas no mesmo numero
        properties_seconds = time.perf_counter() - properties_start

        stats = {
            "model_objects": scanned,
            "assemblies": len(assemblies),
            "expected": len(items),
            "without_tag": no_tag,
            "without_number": no_number,
            "duplicate_numbers": duplicate_numbers,
            "source": source,
            "scan_seconds": round(scan_seconds, 3),
            "properties_seconds": round(properties_seconds, 3),
            "total_seconds": round(time.perf_counter() - start, 3),
        }
        return items, stats

    def expected_subitems_cached(
        self,
        identity: Dict[str, str],
        expected_map: Dict[int, str],
        primary_uda: str,
        fallback_uda: str,
    ) -> Tuple[Dict[Tuple[int, int], str], Dict[str, Any]]:
        key = self.expected_cache_key(identity, primary_uda, fallback_uda) + "|subitems"
        cached = self._expected_subitems_cache.get(key)
        if cached is not None:
            values, stats = cached
            cached_stats = dict(stats)
            cached_stats["cached"] = True
            return dict(values), cached_stats

        start = time.perf_counter()
        expected_parents = set(expected_map)
        subitems: Dict[Tuple[int, int], str] = {}
        scanned = 0
        for obj in self.iter_model_objects():
            scanned += 1
            position = self.subitem_from_model_object(obj)
            if not position:
                continue
            parent, child = position
            if parent not in expected_parents or child <= 0:
                continue
            # Para componentes, a descrição deve ser a da própria peça/subitem
            # (ex.: CH. 10X55X300), e não a descrição geral do conjunto pai
            # (ex.: COAMING PLATE - 1° EL.).
            description = (
                self.subitem_description_from_model_object(obj)
                or self.parent_description_from_model_object(obj)
            )
            current = clean_text(subitems.get((parent, child), ""))
            if description and (not current or len(description) > len(current)):
                subitems[(parent, child)] = description
            else:
                subitems.setdefault((parent, child), "")
        stats = {
            "model_objects": scanned,
            "expected_subitems": len(subitems),
            "cached": False,
            "total_seconds": round(time.perf_counter() - start, 3),
        }
        self._expected_subitems_cache[key] = (dict(subitems), dict(stats))
        return subitems, stats

    def clear_expected_cache(self) -> None:
        self._expected_cache.clear()
        self._expected_subitems_cache.clear()

    def clear_temporary_pages_cache(self) -> None:
        self._temporary_pages_cache.clear()

    @staticmethod
    def expected_cache_key(
        identity: Dict[str, str], primary_uda: str, fallback_uda: str
    ) -> str:
        return "|".join(
            clean_text(value).casefold()
            for value in (
                identity.get("model_path", ""),
                identity.get("model_name", ""),
                identity.get("project_number", ""),
                primary_uda,
                fallback_uda,
            )
        )

    def expected_items_cached(
        self,
        identity: Dict[str, str],
        primary_uda: str,
        fallback_uda: str,
    ) -> Tuple[Dict[int, str], Dict[str, Any]]:
        key = self.expected_cache_key(identity, primary_uda, fallback_uda)
        cached = self._expected_cache.get(key)
        if cached is not None:
            items, stats = cached
            cached_stats = dict(stats)
            cached_stats["cached"] = True
            cached_stats["cache_key"] = key
            return dict(items), cached_stats

        items, stats = self.expected_items(primary_uda, fallback_uda)
        saved_stats = dict(stats)
        saved_stats["cached"] = False
        saved_stats["cache_key"] = key
        self._expected_cache[key] = (dict(items), saved_stats)
        return dict(items), dict(saved_stats)

    def model_object_from_drawing_object(self, drawing_obj: Any):
        if drawing_obj is None:
            return None
        try:
            identifier = drawing_obj.ModelIdentifier
            if identifier is not None:
                return self.model.SelectModelObject(identifier)
        except Exception:
            pass
        # Algumas versões podem retornar diretamente um objeto do modelo.
        try:
            if hasattr(drawing_obj, "GetAssembly"):
                return drawing_obj
        except Exception:
            pass
        return None

    def subitem_from_model_object(self, model_obj: Any) -> Optional[Tuple[int, int]]:
        """Lê a posição exata da peça, sem convertê-la para o número do conjunto.

        A posição decimal (por exemplo 60.1) é a evidência de detalhamento.
        A posição inteira do conjunto (60) é deliberadamente ignorada.
        """
        if model_obj is None:
            return None

        for prop_name in (
            "PART_POS",
            "PART POSITION",
            "PART_POSITION",
            "PART_MARK",
            "POSITION",
        ):
            parsed = parse_subitem_position(self.get_report_property(model_obj, prop_name))
            if parsed:
                return parsed

        # Reserva para versões/modelos em que a série ainda não foi atualizada no relatório.
        try:
            prefix, _start = self.numbering_series_values(getattr(model_obj, "PartNumber"))
            parsed = parse_subitem_position(prefix)
            if parsed:
                return parsed
        except Exception:
            pass
        return None

    def subitem_from_drawing_part(self, drawing_part: Any) -> Optional[Tuple[int, int]]:
        return self.subitem_from_model_object(
            self.model_object_from_drawing_object(drawing_part)
        )

    @staticmethod
    def _element_text(element: Any) -> str:
        """Lê o valor efetivamente apresentado por um elemento de marca.

        A Tekla expõe o conteúdo de marcas como ContainerElement/ElementBase.
        GetUnformattedString devolve o conteúdo sem formatação e evita inferir
        o texto da marca a partir da peça associada.
        """
        if element is None:
            return ""
        try:
            value = clean_text(element.GetUnformattedString())
            if value:
                return value
        except Exception:
            pass
        for attr_name in ("Value", "Text", "TextString"):
            try:
                value = clean_text(getattr(element, attr_name))
                if value:
                    return value
            except Exception:
                pass
        return ""

    def mark_visible_text(self, mark: Any) -> str:
        """Retorna somente o conteúdo visual da marca selecionada no desenho.

        Importante: não usa GetRelatedObjects/PART_POS como evidência. Um balão
        que mostra apenas ``60`` pode estar ligado à peça ``60.1``; nesse caso
        ele deve continuar sendo ignorado.
        """
        if mark is None:
            return ""
        try:
            if bool(getattr(mark, "IsAssociativeNote", False)):
                return ""
        except Exception:
            pass

        try:
            mark.Select()
        except Exception:
            pass

        fragments: List[str] = []
        try:
            attributes = getattr(mark, "Attributes", None)
            content = getattr(attributes, "Content", None) if attributes is not None else None
        except Exception:
            content = None

        # O ContainerElement completo geralmente já devolve o texto final.
        complete = self._element_text(content)
        if complete:
            fragments.append(complete)

        # Reserva para versões em que o container não concatena os filhos.
        for element in iter_net(content):
            value = self._element_text(element)
            if value and value not in fragments:
                fragments.append(value)

        # MarkSet pode manter marcas-filhas com conteúdo próprio.
        try:
            children = mark.GetObjects()
        except Exception:
            children = None
        for child in iter_net(children):
            if child is mark:
                continue
            if object_type_name(child) in {"Mark", "MarkSet"}:
                child_text = self.mark_visible_text(child)
                if child_text and child_text not in fragments:
                    fragments.append(child_text)

        return "\n".join(fragments)

    def subitems_from_mark(self, mark: Any) -> Set[Tuple[int, int]]:
        """Extrai posições decimais exclusivamente do texto visível da marca."""
        found: Set[Tuple[int, int]] = set()
        text = self.mark_visible_text(mark)
        if not text:
            return found
        for match in re.finditer(r"(?<!\d)(\d+)\s*[.,]\s*(\d+)(?!\d)", text):
            try:
                parent = int(match.group(1))
                child = int(match.group(2))
            except Exception:
                continue
            if parent > 0 and child > 0:
                found.add((parent, child))
        return found

    def integer_items_from_mark(self, mark: Any) -> Set[int]:
        found: Set[int] = set()
        text = self.mark_visible_text(mark)
        if not text:
            return found
        for line in re.split(r"[\r\n;]+", text):
            value = clean_text(line)
            if not re.fullmatch(r"\d+", value):
                continue
            try:
                number = int(value)
            except Exception:
                continue
            if number > 0:
                found.add(number)
        return found

    @staticmethod
    def subitem_label(subitem: Tuple[int, int]) -> str:
        return f"{int(subitem[0])}.{int(subitem[1])}"

    @staticmethod
    def view_type_text(view: Any) -> str:
        """Retorna o tipo oficial da vista de forma tolerante entre versões da API."""
        if view is None:
            return ""
        try:
            value = getattr(view, "ViewType", None)
        except Exception:
            value = None
        if value is None:
            try:
                view.Select()
                value = getattr(view, "ViewType", None)
            except Exception:
                value = None
        if value is None:
            return ""
        try:
            return clean_text(value.ToString())
        except Exception:
            return clean_text(value)

    def is_detail_view(self, view: Any) -> bool:
        """Identifica exclusivamente vistas de detalhe nativas do Tekla.

        A propriedade ``View.ViewType`` informa ``DetailView`` (valor 8).
        Essas vistas são ignoradas porque seus balões pertencem ao detalhe de peça
        e não significam que novos conjuntos começaram a ser detalhados.
        """
        type_text = self.view_type_text(view).replace("_", "").replace(" ", "").casefold()
        if type_text.endswith("detailview") or type_text == "8":
            return True
        try:
            value = getattr(view, "ViewType", None)
            numeric = int(getattr(value, "value__", value))
            return numeric == 8
        except Exception:
            return False

    @staticmethod
    def all_objects(container: Any) -> Iterable[Any]:
        enum = None
        for method_name in ("GetAllObjects", "GetObjects"):
            try:
                method = getattr(container, method_name, None)
                if not callable(method):
                    continue
                enum = method()
                if enum is not None:
                    break
            except Exception:
                continue
        if enum is None:
            return
        try:
            while enum.MoveNext():
                yield enum.Current
        except Exception:
            return

    @staticmethod
    def point_xy(point: Any) -> Optional[Tuple[float, float]]:
        if point is None:
            return None
        try:
            return float(point.X), float(point.Y)
        except Exception:
            return None

    def object_box(self, obj: Any) -> Optional[Tuple[float, float, float, float]]:
        """Obtém uma caixa 2D de forma tolerante entre versões da API."""
        if obj is None:
            return None
        for method_name in ("GetAxisAlignedBoundingBox", "GetBoundingBox"):
            try:
                method = getattr(obj, method_name, None)
                if not callable(method):
                    continue
                box = method()
                if box is None:
                    continue
                lower = None
                upper = None
                for name in ("LowerLeft", "MinPoint", "MinimumPoint"):
                    try:
                        lower = getattr(box, name)
                        if lower is not None:
                            break
                    except Exception:
                        pass
                for name in ("UpperRight", "MaxPoint", "MaximumPoint"):
                    try:
                        upper = getattr(box, name)
                        if upper is not None:
                            break
                    except Exception:
                        pass
                p1 = self.point_xy(lower)
                p2 = self.point_xy(upper)
                if p1 and p2:
                    return (
                        min(p1[0], p2[0]), min(p1[1], p2[1]),
                        max(p1[0], p2[0]), max(p1[1], p2[1]),
                    )
            except Exception:
                pass

        # Algumas versões expõem a caixa como propriedade.
        for box_name in ("RestrictionBox", "BoundingBox"):
            try:
                box = getattr(obj, box_name)
            except Exception:
                box = None
            if box is None:
                continue
            lower = upper = None
            for name in ("LowerLeft", "MinPoint", "MinimumPoint"):
                try:
                    lower = getattr(box, name)
                    if lower is not None:
                        break
                except Exception:
                    pass
            for name in ("UpperRight", "MaxPoint", "MaximumPoint"):
                try:
                    upper = getattr(box, name)
                    if upper is not None:
                        break
                except Exception:
                    pass
            p1 = self.point_xy(lower)
            p2 = self.point_xy(upper)
            if p1 and p2:
                return (
                    min(p1[0], p2[0]), min(p1[1], p2[1]),
                    max(p1[0], p2[0]), max(p1[1], p2[1]),
                )

        points: List[Tuple[float, float]] = []
        for attr_name in (
            "StartPoint", "EndPoint", "LowerLeft", "UpperRight",
            "InsertionPoint", "Origin", "Position",
        ):
            try:
                point = self.point_xy(getattr(obj, attr_name))
                if point:
                    points.append(point)
            except Exception:
                pass
        if points:
            xs = [point[0] for point in points]
            ys = [point[1] for point in points]
            return min(xs), min(ys), max(xs), max(ys)
        return None

    def object_point(self, obj: Any) -> Optional[Tuple[float, float]]:
        for attr_name in ("Origin", "InsertionPoint", "Position"):
            try:
                point = self.point_xy(getattr(obj, attr_name))
                if point:
                    return point
            except Exception:
                pass
        box = self.object_box(obj)
        if box:
            return (box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0
        return None

    @staticmethod
    def is_dimension_object(obj: Any) -> bool:
        type_name = object_type_name(obj)
        return any(hint in type_name for hint in DIMENSION_TYPE_HINTS)

    @staticmethod
    def related_objects(obj: Any) -> Iterable[Any]:
        if obj is None:
            return
        for method_name in ("GetRelatedObjects", "GetObjects"):
            try:
                method = getattr(obj, method_name, None)
                if not callable(method):
                    continue
                collection = method()
            except Exception:
                continue
            for related in iter_net(collection):
                yield related

    def subitems_from_related_object(self, obj: Any) -> Set[Tuple[int, int]]:
        found: Set[Tuple[int, int]] = set()
        if obj is None:
            return found
        if object_type_name(obj) == "Part":
            subitem = self.subitem_from_drawing_part(obj)
            if subitem:
                found.add(subitem)
                return found
        subitem = self.subitem_from_model_object(self.model_object_from_drawing_object(obj))
        if subitem:
            found.add(subitem)
        return found

    def dimension_related_subitems(self, dimension_obj: Any) -> Set[Tuple[int, int]]:
        found: Set[Tuple[int, int]] = set()
        seen: Set[str] = set()

        def visit(obj: Any, depth: int) -> None:
            if obj is None or depth > 3:
                return
            key = object_identifier_key(obj) or f"OBJ={id(obj)}"
            visit_key = f"{depth}:{key}"
            if visit_key in seen:
                return
            seen.add(visit_key)
            found.update(self.subitems_from_related_object(obj))
            for related in self.related_objects(obj):
                if related is obj:
                    continue
                visit(related, depth + 1)

        visit(dimension_obj, 0)
        return found

    def view_display_name(self, view: Any, fallback_index: int) -> str:
        for attr_name in ("Name", "Title", "Label"):
            try:
                value = clean_text(getattr(view, attr_name))
            except Exception:
                value = ""
            if value:
                return value
        return f"Vista {fallback_index}"

    @staticmethod
    def normalize_view_name(value: Any) -> str:
        """Normaliza um nome de vista removendo espacos e sinais."""
        text = clean_text(value).casefold()
        if not text:
            return ""
        return re.sub(r"[^a-z0-9]+", "", text)

    @classmethod
    def view_name_aliases(cls, value: Any) -> Set[str]:
        """Cria aliases seguros para o nome visivel/real de uma vista.

        No Tekla, o que aparece na folha pode ser ``CORTE " A - A "`` enquanto
        ``View.Name`` pode retornar ``A-A`` ou apenas ``A``. O mesmo ocorre com
        nomes como ``AC-AC``. Esta rotina permite que o usuario informe o nome
        que enxerga na folha sem depender da representacao interna da API.
        """
        raw = clean_text(value).casefold()
        if not raw:
            return set()

        normalized = cls.normalize_view_name(raw)
        aliases: Set[str] = {normalized} if normalized else set()

        # Prefixos que o rotulo da vista pode exibir, mas que nao fazem parte do
        # nome interno da View no Tekla.
        prefix_pattern = r"^\s*(?:corte|section|secao|seção|vista|view|detalhe|detail)\b[\s:._\-\"']*"
        stripped = re.sub(prefix_pattern, "", raw, flags=re.IGNORECASE).strip()
        stripped_normalized = cls.normalize_view_name(stripped)
        if stripped_normalized:
            aliases.add(stripped_normalized)

        # Vistas de secao podem aparecer como A-A/AC-AC, enquanto a API retorna
        # somente A/AC. Aceita a metade apenas quando o texto e uma repeticao
        # exata, evitando correspondencias parciais perigosas.
        for token in list(aliases):
            if len(token) >= 2 and len(token) % 2 == 0:
                half = token[: len(token) // 2]
                if half and token == half + half:
                    aliases.add(half)

        return {alias for alias in aliases if alias}

    @classmethod
    def view_label_aliases_from_text(cls, value: Any) -> Set[str]:
        """Extrai aliases de textos visiveis que podem ser rotulos de vista."""
        raw = clean_text(value)
        if not raw:
            return set()

        text_variants = [raw]
        simplified = re.sub(r"[{}|]+", " ", raw)
        simplified = re.sub(r"\s+", " ", simplified).strip()
        if simplified and simplified not in text_variants:
            text_variants.append(simplified)

        aliases: Set[str] = set()
        for text in text_variants:
            aliases.update(cls.view_name_aliases(text))
        if not aliases:
            return set()

        lower = " ".join(text_variants).casefold()
        has_view_hint = bool(
            re.search(r"\b(corte|section|secao|seção|vista|view|detalhe|detail)\b", lower)
        )
        has_section_separator = bool(
            re.search(r"[a-z0-9]{1,6}\s*-\s*[a-z0-9]{1,6}", lower)
        )
        if has_view_hint or has_section_separator:
            return aliases

        # Evita que textos curtos soltos como "A" ou "BL" ignorem uma vista por
        # acidente. Rotulos como "CORTE BL - BL" passam pelo criterio acima.
        return {alias for alias in aliases if len(alias) >= 3}

    @classmethod
    def configured_base_view_names(cls, value: Any) -> Set[str]:
        """Converte a configuracao em todos os aliases aceitos de vistas-base."""
        entries: List[Any]
        if isinstance(value, (list, tuple, set)):
            entries = list(value)
        elif value is None:
            entries = []
        else:
            entries = re.split(r"[\r\n;,]+", clean_text(value))

        aliases: Set[str] = set()
        for entry in entries:
            aliases.update(cls.view_name_aliases(entry))
        return aliases

    def drawing_object_text_values(self, obj: Any) -> List[str]:
        """Le textos diretos/atributos de um objeto de desenho de forma tolerante."""
        texts: List[str] = []

        def add_text(value: Any) -> None:
            text = clean_text(value).replace("\r", " ").replace("\n", " ")
            text = re.sub(r"\s+", " ", text).strip()
            if text and text not in texts:
                texts.append(text)

        add_text(self.drawing_text_value(obj))

        try:
            attributes = getattr(obj, "Attributes", None)
            content = getattr(attributes, "Content", None) if attributes is not None else None
        except Exception:
            content = None

        add_text(self._element_text(content))
        for element in iter_net(content):
            add_text(self._element_text(element))

        if object_type_name(obj) in {"Mark", "MarkSet"}:
            add_text(self.mark_visible_text(obj))

        return texts

    def view_attribute_text_values(self, view: Any) -> List[str]:
        """Le o label automatico da vista nos atributos TagA/TagB do Tekla."""
        texts: List[str] = []

        def add_text(value: Any) -> None:
            text = clean_text(value).replace("\r", " ").replace("\n", " ")
            text = re.sub(r"\s+", " ", text).strip()
            if text and text not in texts:
                texts.append(text)

        try:
            attributes = getattr(view, "Attributes", None)
            tags = getattr(attributes, "TagsAttributes", None) if attributes is not None else None
        except Exception:
            tags = None
        if tags is None:
            return texts

        tag_objects: List[Any] = []
        try:
            properties = list(tags.GetType().GetProperties())
        except Exception:
            properties = []
        for prop in properties:
            try:
                name = clean_text(prop.Name)
                if not name.startswith("Tag"):
                    continue
                tag_obj = prop.GetValue(tags, None)
            except Exception:
                continue
            if tag_obj is not None:
                tag_objects.append(tag_obj)

        for tag_obj in tag_objects:
            try:
                content = getattr(tag_obj, "TagContent", None)
            except Exception:
                content = None
            add_text(self._element_text(content))
            for element in iter_net(content):
                add_text(self._element_text(element))

        return texts

    def view_visible_name_aliases(self, view: Any) -> Tuple[Set[str], List[str]]:
        """Le textos dentro da vista para reconhecer rotulos como CORTE "BL - BL"."""
        aliases: Set[str] = set()
        labels: List[str] = []

        for text in self.view_attribute_text_values(view):
            text_aliases = self.view_label_aliases_from_text(text)
            if not text_aliases:
                continue
            aliases.update(text_aliases)
            if text not in labels:
                labels.append(text)

        for obj in self.all_objects(view):
            for text in self.drawing_object_text_values(obj):
                text_aliases = self.view_label_aliases_from_text(text)
                if not text_aliases:
                    continue
                aliases.update(text_aliases)
                if text not in labels:
                    labels.append(text)

        return aliases, labels

    def is_component_base_view_ignored(
        self,
        view: Any,
        fallback_index: int,
        ignored_names: Set[str],
    ) -> Tuple[bool, str]:
        """Indica se a vista foi marcada pelo usuario como vista-base.

        No modo Por conjunto, vistas-base continuam valendo para a medicao e
        para o rastreamento de tempo (regra historica).

        No modo Por componente, alem de nao contarem como evidencia de conclusao
        do componente, essas vistas tambem NAO marcam o conjunto como iniciado:
        um cutout que so aparece nos cortes da planta baixa nao pode entrar em
        "Em detalhamento" nem iniciar a contagem de tempo sem o detalhe ter
        realmente comecado.
        """
        view_name = self.view_display_name(view, fallback_index)
        aliases = self.view_name_aliases(view_name)
        if aliases.intersection(ignored_names):
            return True, view_name

        visible_aliases, visible_labels = self.view_visible_name_aliases(view)
        if visible_aliases.intersection(ignored_names):
            for label in visible_labels:
                if self.view_label_aliases_from_text(label).intersection(ignored_names):
                    return True, label
            return True, view_name

        return False, view_name

    def analyze_view_dimensions(
        self,
        view: Any,
        view_index: int,
        expected_map: Dict[int, str],
        page_number: Optional[int] = None,
    ) -> Dict[str, Any]:
        view_name = self.view_display_name(view, view_index)
        required: Dict[Tuple[int, int], Dict[str, Any]] = {}
        dimensioned: Set[Tuple[int, int]] = set()
        ignored_integer_items: Set[int] = set()
        integer_items: Set[int] = set()
        marks_count = 0
        dimension_objs: List[Any] = []

        for obj in self.all_objects(view):
            type_name = object_type_name(obj)
            if type_name in {"Mark", "MarkSet"}:
                marks_count += 1
                for subitem in self.subitems_from_mark(obj):
                    parent, child = subitem
                    if parent not in expected_map:
                        continue
                    required.setdefault(
                        subitem,
                        {
                            "number": parent,
                            "child": child,
                            "label": self.subitem_label(subitem),
                            "tag": expected_map.get(parent, ""),
                            "view": view_name,
                            "page": page_number,
                        },
                    )
                integers = self.integer_items_from_mark(obj)
                ignored_integer_items.update(integers)
                integer_items.update(
                    number for number in integers if number in expected_map
                )
                continue

            if self.is_dimension_object(obj):
                dimension_objs.append(obj)

        required_keys = set(required)
        if required_keys:
            for dimension_obj in dimension_objs:
                dimensioned.update(
                    self.dimension_related_subitems(dimension_obj) & required_keys
                )
                if required_keys.issubset(dimensioned):
                    break

        missing = [
            entry
            for subitem, entry in sorted(required.items(), key=lambda item: item[0])
            if subitem not in dimensioned
        ]
        required_parents = {parent for parent, _child in required}
        cut_missing = [
            {
                "number": number,
                "tag": expected_map.get(number, ""),
                "view": view_name,
                "page": page_number,
                "message": "sem subitem",
            }
            for number in sorted(integer_items - required_parents)
        ]
        ok_count = max(0, len(required) - len(missing))
        return {
            "view": view_name,
            "page": page_number,
            "required_count": len(required),
            "dimensioned_count": ok_count,
            "missing_count": len(missing),
            "cut_missing_count": len(cut_missing),
            "dimension_objects": len(dimension_objs),
            "marks": marks_count,
            "ignored_integer_items": numeric_sort(ignored_integer_items),
            "missing": missing,
            "cut_missing": cut_missing,
        }

    @staticmethod
    def drawing_text_value(obj: Any) -> str:
        for attr_name in ("TextString", "Text", "Content", "Value"):
            try:
                value = getattr(obj, attr_name, None)
                if value is not None and not callable(value):
                    text = clean_text(value).replace("\r", " ").replace("\n", " ")
                    text = re.sub(r"\s+", " ", text).strip()
                    if text:
                        return text
            except Exception:
                pass
        return ""

    def line_points(self, obj: Any) -> Optional[Tuple[float, float, float, float]]:
        try:
            p1 = self.point_xy(getattr(obj, "StartPoint"))
            p2 = self.point_xy(getattr(obj, "EndPoint"))
            if p1 and p2:
                return p1[0], p1[1], p2[0], p2[1]
        except Exception:
            pass
        return None

    def _detect_page_rectangles(
        self, sheet: Any, width: float, height: float
    ) -> List[Dict[str, float]]:
        tolerance = 2.0
        horizontal: List[Tuple[float, float, float]] = []
        vertical: List[Tuple[float, float, float]] = []
        for obj in self.all_objects(sheet):
            points = self.line_points(obj)
            if not points:
                continue
            x1, y1, x2, y2 = points
            if (
                abs(y1 - y2) <= tolerance
                and abs(abs(x2 - x1) - width) <= max(5.0, width * 0.02)
            ):
                horizontal.append((min(x1, x2), y1, max(x1, x2)))
            elif (
                abs(x1 - x2) <= tolerance
                and abs(abs(y2 - y1) - height) <= max(5.0, height * 0.02)
            ):
                vertical.append((x1, min(y1, y2), max(y1, y2)))

        rectangles: List[Dict[str, float]] = []
        for left, bottom, right in horizontal:
            top = bottom + height
            has_top = any(
                abs(item[0] - left) <= tolerance
                and abs(item[2] - right) <= tolerance
                and abs(item[1] - top) <= tolerance
                for item in horizontal
            )
            has_left = any(
                abs(item[0] - left) <= tolerance
                and abs(item[1] - bottom) <= tolerance
                and abs(item[2] - top) <= tolerance
                for item in vertical
            )
            has_right = any(
                abs(item[0] - right) <= tolerance
                and abs(item[1] - bottom) <= tolerance
                and abs(item[2] - top) <= tolerance
                for item in vertical
            )
            if has_top and has_left and has_right:
                rectangles.append(
                    {"left": left, "bottom": bottom, "right": right, "top": top}
                )

        unique: List[Dict[str, float]] = []
        for rectangle in sorted(
            rectangles, key=lambda item: (-item["top"], item["left"])
        ):
            if not any(
                abs(rectangle["left"] - saved["left"]) <= tolerance
                and abs(rectangle["bottom"] - saved["bottom"]) <= tolerance
                for saved in unique
            ):
                unique.append(rectangle)
        return unique

    @staticmethod
    def _point_inside_page(
        point: Tuple[float, float], page: Dict[str, Any], margin: float = 8.0
    ) -> bool:
        x, y = point
        return (
            float(page["left"]) - margin <= x <= float(page["right"]) + margin
            and float(page["bottom"]) - margin <= y <= float(page["top"]) + margin
        )

    def detect_temporary_pages(self, sheet: Any) -> List[Dict[str, Any]]:
        """Reconhece as margens temporárias pelo texto da legenda, como ``5 / 33``."""
        width = TEMP_PAGE_WIDTH
        height = TEMP_PAGE_HEIGHT
        rectangles = self._detect_page_rectangles(sheet, width, height)
        pages: List[Dict[str, Any]] = []
        seen: Set[int] = set()

        for obj in self.all_objects(sheet):
            text = self.drawing_text_value(obj)
            match = re.match(r"^\s*(\d+)\s*/\s*(\d+)\s*$", text)
            if not match:
                continue
            number = int(match.group(1))
            total = int(match.group(2))
            if number <= 0 or number in seen:
                continue
            point = self.object_point(obj)
            rectangle = None
            if point and rectangles:
                containing = [
                    item for item in rectangles if self._point_inside_page(point, item, 20.0)
                ]
                candidates = containing or rectangles
                rectangle = min(
                    candidates,
                    key=lambda item: abs((item["right"] - width * 0.18) - point[0])
                    + abs((item["bottom"] + height * 0.12) - point[1]),
                )
            if rectangle is None and point:
                # Reserva: a legenda fica no canto inferior direito da margem A1.
                rectangle = {
                    "left": point[0] - width * 0.82,
                    "bottom": point[1] - height * 0.12,
                    "right": point[0] + width * 0.18,
                    "top": point[1] + height * 0.88,
                }
            if rectangle is None:
                continue
            pages.append(
                {
                    "number": number,
                    "total": total,
                    "left": float(rectangle["left"]),
                    "bottom": float(rectangle["bottom"]),
                    "right": float(rectangle["right"]),
                    "top": float(rectangle["top"]),
                }
            )
            seen.add(number)
        return sorted(pages, key=lambda page: int(page["number"]))

    @staticmethod
    def temporary_pages_cache_key(identity: Optional[Dict[str, str]]) -> str:
        if not isinstance(identity, dict):
            return ""
        return "|".join(
            clean_text(value).casefold()
            for value in (
                identity.get("project_key", ""),
                identity.get("drawing_name", ""),
                identity.get("model_path", ""),
            )
        )

    def detect_temporary_pages_cached(
        self,
        sheet: Any,
        identity: Optional[Dict[str, str]] = None,
        refresh: bool = False,
    ) -> Tuple[List[Dict[str, Any]], bool]:
        key = self.temporary_pages_cache_key(identity)
        if key and not refresh and key in self._temporary_pages_cache:
            return [dict(page) for page in self._temporary_pages_cache[key]], True
        pages = self.detect_temporary_pages(sheet)
        if key:
            self._temporary_pages_cache[key] = [dict(page) for page in pages]
        return pages, False

    @staticmethod
    def page_from_label_point(number: int, total: int, point: Tuple[float, float]) -> Dict[str, Any]:
        return {
            "number": int(number),
            "total": int(total),
            "left": float(point[0] - TEMP_PAGE_WIDTH * 0.82),
            "bottom": float(point[1] - TEMP_PAGE_HEIGHT * 0.12),
            "right": float(point[0] + TEMP_PAGE_WIDTH * 0.18),
            "top": float(point[1] + TEMP_PAGE_HEIGHT * 0.88),
        }

    def detect_temporary_pages_for_numbers(
        self,
        sheet: Any,
        page_numbers: Iterable[int],
        identity: Optional[Dict[str, str]] = None,
    ) -> Tuple[List[Dict[str, Any]], str]:
        wanted: Set[int] = set()
        for value in page_numbers:
            try:
                number = int(value)
            except Exception:
                continue
            if number > 0:
                wanted.add(number)
        if not wanted:
            pages, cached = self.detect_temporary_pages_cached(sheet, identity)
            return pages, "cache" if cached else "full"

        key = self.temporary_pages_cache_key(identity)
        cached_pages = self._temporary_pages_cache.get(key, []) if key else []
        selected = [
            dict(page)
            for page in cached_pages
            if int(page.get("number", 0) or 0) in wanted
        ]
        if wanted.issubset({int(page.get("number", 0) or 0) for page in selected}):
            return selected, "cache_selected"

        pages: List[Dict[str, Any]] = []
        seen: Set[int] = set()
        for obj in self.all_objects(sheet):
            text = self.drawing_text_value(obj)
            match = re.match(r"^\s*(\d+)\s*/\s*(\d+)\s*$", text)
            if not match:
                continue
            number = int(match.group(1))
            if number not in wanted or number in seen:
                continue
            point = self.object_point(obj)
            if not point:
                continue
            pages.append(self.page_from_label_point(number, int(match.group(2)), point))
            seen.add(number)
            if wanted.issubset(seen):
                break

        if pages:
            if key:
                merged = {
                    int(page.get("number", 0) or 0): dict(page)
                    for page in cached_pages
                }
                for page in pages:
                    merged[int(page["number"])] = dict(page)
                self._temporary_pages_cache[key] = sorted(
                    merged.values(), key=lambda page: int(page["number"])
                )
            return sorted(pages, key=lambda page: int(page["number"])), "label_selected"

        pages, cached = self.detect_temporary_pages_cached(sheet, identity)
        return [
            page for page in pages if int(page.get("number", 0) or 0) in wanted
        ], "cache" if cached else "full"

    def page_for_view(
        self, view: Any, pages: List[Dict[str, Any]]
    ) -> Optional[int]:
        if not pages:
            return None
        candidates: List[Tuple[float, float]] = []
        # A origem da View é o ponto mais confiável em coordenadas da folha.
        for attr_name in ("Origin", "InsertionPoint", "Position"):
            try:
                point = self.point_xy(getattr(view, attr_name))
                if point and point not in candidates:
                    candidates.append(point)
            except Exception:
                pass
        for point in candidates:
            for page in pages:
                if self._point_inside_page(point, page):
                    return int(page["number"])

        box = self.object_box(view)
        if box:
            center = ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)
            if center not in candidates:
                candidates.append(center)

        for point in candidates:
            for page in pages:
                if self._point_inside_page(point, page):
                    return int(page["number"])

        # Só usa proximidade quando o ponto está realmente junto de uma margem.
        # Isso evita associar uma View cujo Origin esteja em coordenadas do modelo.
        for point in candidates:
            ranked: List[Tuple[float, Dict[str, Any]]] = []
            for page in pages:
                dx = max(
                    float(page["left"]) - point[0],
                    0.0,
                    point[0] - float(page["right"]),
                )
                dy = max(
                    float(page["bottom"]) - point[1],
                    0.0,
                    point[1] - float(page["top"]),
                )
                ranked.append((dx + dy, page))
            distance, nearest = min(ranked, key=lambda entry: entry[0])
            if distance <= 60.0:
                return int(nearest["number"])
        return None

    def scan_drawing(
        self,
        drawing: Any,
        expected_map: Dict[int, str],
        identity: Optional[Dict[str, str]] = None,
        ignored_component_views: Optional[Iterable[str]] = None,
        progress_mode: str = "conjunto",
    ) -> Tuple[
        Set[int],
        Dict[int, Set[str]],
        Dict[int, Set[int]],
        Set[Tuple[int, int]],
        Dict[Tuple[int, int], Set[int]],
        Dict[Tuple[int, int], Set[str]],
        Dict[str, Any],
    ]:
        """Lê marcas nas vistas válidas e associa cada item à margem temporária.

        No modo por conjunto, vistas nativas do tipo DetailView continuam sendo
        descartadas como evidência de início, preservando a regra histórica que
        evita abrir um conjunto apenas porque um detalhe isolado foi criado.

        No modo por componente, porém, DetailView é justamente uma evidência
        válida de que aquele componente recebeu detalhamento. Nesse modo seus
        subitens entram apenas em ``decimal_positions``/``subitem_pages``; eles
        NÃO entram em ``marked`` e, portanto, não alteram o rastreamento de tempo
        nem a lógica antiga do progresso por conjunto.

        Vistas-base configuradas pelo usuário (por exemplo VISTA FRONTAL ou A-A)
        nunca comprovam conclusão no modo por componente.
        """
        start = time.perf_counter()
        sheet = drawing.GetSheet()
        if sheet is None:
            raise RuntimeError("Não foi possível acessar a folha do multidesenho.")

        expected = set(expected_map)
        subitem_mode = clean_text(progress_mode).casefold() == "subitem"
        marked: Set[int] = set()
        item_views: Dict[int, Set[str]] = {}
        item_pages: Dict[int, Set[int]] = {}
        views_count = 0
        analyzed_views_count = 0
        ignored_detail_views_count = 0
        component_detail_views_used = 0
        ignored_detail_items: Set[int] = set()
        marks_count = 0
        drawing_parts_count = 0
        decimal_positions: Set[Tuple[int, int]] = set()
        subitem_pages: Dict[Tuple[int, int], Set[int]] = {}
        subitem_views: Dict[Tuple[int, int], Set[str]] = {}
        marks_with_decimal = 0
        marks_without_decimal = 0
        ignored_component_names = self.configured_base_view_names(ignored_component_views)
        ignored_component_views_count = 0
        ignored_component_view_names: Set[str] = set()
        ignored_component_positions: Set[Tuple[int, int]] = set()
        pages_start = time.perf_counter()
        temporary_pages, temporary_pages_cached = self.detect_temporary_pages_cached(
            sheet, identity, refresh=True
        )
        pages_seconds = time.perf_counter() - pages_start
        views_with_page = 0

        try:
            views_enum = sheet.GetViews()
        except Exception as exc:
            raise RuntimeError(f"Não foi possível ler as vistas do multidesenho: {exc}")

        views_start = time.perf_counter()
        while views_enum.MoveNext():
            view = views_enum.Current
            views_count += 1
            view_page_number = self.page_for_view(view, temporary_pages)

            is_native_detail = self.is_detail_view(view)
            component_only_detail_view = False
            if is_native_detail:
                ignored_detail_views_count += 1
                if clean_text(progress_mode).casefold() == "subitem":
                    # Para medição por componente, uma DetailView é evidência
                    # válida de conclusão do componente, mas NÃO deve iniciar
                    # conjunto nem interferir no rastreamento de tempo.
                    component_only_detail_view = True
                    component_detail_views_used += 1
                else:
                    # Regra histórica do modo por conjunto: detalhe isolado não
                    # comprova início do conjunto.
                    for detail_obj in self.all_objects(view):
                        if object_type_name(detail_obj) not in {"Mark", "MarkSet"}:
                            continue
                        for parent, _child in self.subitems_from_mark(detail_obj):
                            if parent in expected:
                                ignored_detail_items.add(parent)
                    continue

            analyzed_views_count += 1
            ignore_for_component, view_display_name = self.is_component_base_view_ignored(
                view, views_count, ignored_component_names
            )
            if ignore_for_component:
                ignored_component_views_count += 1
                ignored_component_view_names.add(view_display_name)

            view_key = object_identifier_key(view)
            if not view_key:
                view_name = clean_text(getattr(view, "Name", ""))
                view_key = f"VIEW={views_count}:{view_name}"
            page_number = view_page_number
            if page_number is not None:
                views_with_page += 1

            for obj in self.all_objects(view):
                type_name = object_type_name(obj)

                if type_name in {"Mark", "MarkSet"}:
                    marks_count += 1
                    subitems = self.subitems_from_mark(obj)
                    if subitems:
                        marks_with_decimal += 1
                    else:
                        marks_without_decimal += 1
                    for parent, child in subitems:
                        position = (parent, child)
                        if ignore_for_component:
                            ignored_component_positions.add(position)
                        else:
                            decimal_positions.add(position)
                        if parent in expected:
                            # Regra de quando o conjunto entra em "Em detalhamento"
                            # (e passa a ter tempo contado).
                            #
                            # Modo Por componente: o conjunto conta assim que
                            # qualquer componente seu aparece numa vista real de
                            # detalhamento (inclusive DetailView, que é a evidência
                            # principal do componente), MAS nunca a partir de uma
                            # vista da lista de exceção (cortes da planta baixa como
                            # B-B/C-C), onde o cutout aparece sem o detalhe ter
                            # começado.
                            #
                            # Modo Por conjunto: regra histórica preservada — a
                            # DetailView já foi descartada antes e as vistas-base
                            # continuam valendo.
                            if subitem_mode:
                                mark_conjunto = not ignore_for_component
                            else:
                                mark_conjunto = not component_only_detail_view
                            if mark_conjunto:
                                marked.add(parent)
                                item_views.setdefault(parent, set()).add(view_key)
                                if page_number is not None:
                                    item_pages.setdefault(parent, set()).add(page_number)
                            if not ignore_for_component:
                                subitem_views.setdefault(position, set()).add(view_key)
                                if page_number is not None:
                                    subitem_pages.setdefault(position, set()).add(page_number)
                    continue

                if type_name == "Part":
                    drawing_parts_count += 1

        stats = {
            "views": views_count,
            "analyzed_views": analyzed_views_count,
            "ignored_detail_views": ignored_detail_views_count,
            "component_detail_views_used": component_detail_views_used,
            "ignored_detail_items": numeric_sort(ignored_detail_items),
            "ignored_component_views": ignored_component_views_count,
            "ignored_component_view_names": sorted(ignored_component_view_names, key=str.casefold),
            "ignored_component_positions": len(ignored_component_positions),
            "views_with_page": views_with_page,
            "temporary_pages": len(temporary_pages),
            "temporary_pages_cached": bool(temporary_pages_cached),
            "marks": marks_count,
            "marks_with_decimal": marks_with_decimal,
            "marks_without_decimal": marks_without_decimal,
            "drawing_parts": drawing_parts_count,
            "decimal_positions": len(decimal_positions),
            "marked_items": len(marked),
            "items_with_page": len(item_pages),
            "pages_seconds": round(pages_seconds, 3),
            "views_seconds": round(time.perf_counter() - views_start, 3),
            "total_seconds": round(time.perf_counter() - start, 3),
        }
        return marked, item_views, item_pages, decimal_positions, subitem_pages, subitem_views, stats

    @staticmethod
    def empty_cota_summary(
        scope: str = "no_active_page",
        scope_pages: Optional[Iterable[int]] = None,
        message: str = "",
    ) -> Dict[str, Any]:
        return {
            "scope": scope,
            "scope_pages": numeric_sort(scope_pages or []),
            "views_checked": 0,
            "required_subitems": 0,
            "dimension_objects": 0,
            "missing_count": 0,
            "cut_missing_count": 0,
            "pending_views_count": 0,
            "ignored_integer_items": [],
            "views": [],
            "missing": [],
            "cut_missing": [],
            "message": message,
        }

    def scan_cota_drawing(
        self,
        drawing: Any,
        expected_map: Dict[int, str],
        focus_pages: Optional[Iterable[int]] = None,
        identity: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        start = time.perf_counter()
        sheet = drawing.GetSheet()
        if sheet is None:
            raise RuntimeError("Nao foi possivel acessar a folha do multidesenho.")

        selected_pages = {
            int(value)
            for value in (focus_pages or [])
            if str(value).isdigit() and int(value) > 0
        }
        scope = "active_pages" if selected_pages else "no_active_page"
        page_detection_source = "full"
        pages_start = time.perf_counter()
        if selected_pages:
            temporary_pages, page_detection_source = self.detect_temporary_pages_for_numbers(
                sheet, selected_pages, identity
            )
        else:
            temporary_pages, cached = self.detect_temporary_pages_cached(sheet, identity)
            page_detection_source = "cache" if cached else "full"
        pages_seconds = time.perf_counter() - pages_start

        if not selected_pages:
            detected_pages = {
                int(page.get("number"))
                for page in temporary_pages
                if str(page.get("number")).isdigit()
            }
            if len(detected_pages) == 1:
                selected_pages = detected_pages
                scope = "single_page"
            else:
                summary = self.empty_cota_summary(
                    scope,
                    [],
                    "Folha atual nao identificada para a analise de pendencias.",
                )
                summary.update(
                    {
                        "page_detection_source": page_detection_source,
                        "pages_seconds": round(pages_seconds, 3),
                        "views_seconds": 0.0,
                        "total_seconds": round(time.perf_counter() - start, 3),
                    }
                )
                return summary

        cota_views: List[Dict[str, Any]] = []
        cota_missing: List[Dict[str, Any]] = []
        cota_cut_missing: List[Dict[str, Any]] = []
        cota_ignored_integer_items: Set[int] = set()
        cota_dimension_objects = 0
        cota_required_subitems = 0

        try:
            views_enum = sheet.GetViews()
        except Exception as exc:
            raise RuntimeError(f"Nao foi possivel ler as vistas do multidesenho: {exc}")

        views_start = time.perf_counter()
        view_index = 0
        while views_enum.MoveNext():
            view = views_enum.Current
            view_index += 1
            page_number = self.page_for_view(view, temporary_pages)
            if page_number not in selected_pages:
                continue

            cota_view = self.analyze_view_dimensions(
                view, view_index, expected_map, page_number
            )
            cota_views.append(cota_view)
            cota_missing.extend(cota_view.get("missing", []))
            cota_cut_missing.extend(cota_view.get("cut_missing", []))
            cota_ignored_integer_items.update(cota_view.get("ignored_integer_items", []))
            cota_dimension_objects += int(cota_view.get("dimension_objects", 0) or 0)
            cota_required_subitems += int(cota_view.get("required_count", 0) or 0)

        pending_views = {
            (str(item.get("view") or ""), int(item.get("page") or 0))
            for item in [*cota_missing, *cota_cut_missing]
            if isinstance(item, dict)
        }

        return {
            "scope": scope,
            "scope_pages": numeric_sort(selected_pages),
            "page_detection_source": page_detection_source,
            "views_checked": len(cota_views),
            "required_subitems": cota_required_subitems,
            "dimension_objects": cota_dimension_objects,
            "missing_count": len(cota_missing),
            "cut_missing_count": len(cota_cut_missing),
            "pending_views_count": len(pending_views),
            "ignored_integer_items": numeric_sort(cota_ignored_integer_items),
            "views": cota_views,
            "missing": cota_missing,
            "cut_missing": cota_cut_missing,
            "pages_seconds": round(pages_seconds, 3),
            "views_seconds": round(time.perf_counter() - views_start, 3),
            "total_seconds": round(time.perf_counter() - start, 3),
            "message": "",
        }

    def analyze_cota(
        self,
        config: Dict[str, Any],
        expected_map: Dict[int, str],
        focus_pages: Optional[Iterable[int]] = None,
    ) -> Dict[str, Any]:
        drawing = self.require_active_drawing(clean_text(config.get("tekla_root")))
        identity = self.model_identity(drawing)
        expected_source = "context"
        if not expected_map:
            primary = clean_text(config.get("uda_principal")) or "FRMW"
            fallback = clean_text(config.get("uda_reserva")) or "AwevaFRMW"
            expected_map, model_stats = self.expected_items_cached(
                identity, primary, fallback
            )
            expected_source = "cache" if model_stats.get("cached") else "model"
        if not expected_map:
            raise RuntimeError("Nenhum conjunto numerado foi encontrado para conferir pendencias.")

        summary = self.scan_cota_drawing(drawing, expected_map, focus_pages, identity)
        summary["expected_source"] = expected_source
        summary["expected_items"] = len(expected_map)
        return {
            "ok": True,
            "project_key": identity["project_key"],
            "project_number": identity["project_number"],
            "drawing_name": identity["drawing_name"],
            "cota_expected_source": expected_source,
            "cota_summary": summary,
            "cota_items": summary.get("missing", []),
        }

    def analyze(self, config: Dict[str, Any]) -> Dict[str, Any]:
        total_start = time.perf_counter()
        connect_start = time.perf_counter()
        drawing = self.require_active_drawing(clean_text(config.get("tekla_root")))
        connect_seconds = time.perf_counter() - connect_start
        identity = self.model_identity(drawing)
        primary = clean_text(config.get("uda_principal")) or "FRMW"
        fallback = clean_text(config.get("uda_reserva")) or "AwevaFRMW"
        self._log("Lendo conjuntos esperados no modelo...")
        model_start = time.perf_counter()
        expected_map, model_stats = self.expected_items_cached(
            identity, primary, fallback
        )
        model_seconds = time.perf_counter() - model_start
        expected = set(expected_map)
        if not expected:
            raise RuntimeError(
                "Nenhum conjunto numerado com TAG válida foi encontrado no modelo. "
                "Confira a numeração e os campos de TAG nas configurações."
            )

        mode = clean_text(config.get("modo_progresso")).casefold() or "conjunto"
        if mode not in {"conjunto", "subitem"}:
            mode = "conjunto"

        self._log("Lendo marcas e vistas do multidesenho...")
        drawing_start = time.perf_counter()
        marked, item_views, item_pages, detected_subitems, subitem_pages, subitem_views, drawing_stats = self.scan_drawing(
            drawing,
            expected_map,
            identity,
            config.get("vistas_base_ignoradas", []),
            mode,
        )
        drawing_seconds = time.perf_counter() - drawing_start
        detailed = marked & expected
        missing = expected - detailed
        expected_subitems: Dict[Tuple[int, int], str] = {}
        subitem_stats: Dict[str, Any] = {}
        detailed_subitems: Set[Tuple[int, int]] = set()
        missing_subitems: Set[Tuple[int, int]] = set()
        component_max_by_parent: Dict[int, int] = {}
        component_detailed_by_parent: Dict[int, Set[int]] = {}
        if mode == "subitem":
            expected_subitems, subitem_stats = self.expected_subitems_cached(
                identity, expected_map, primary, fallback
            )
            if not expected_subitems:
                raise RuntimeError(
                    "Nenhum componente decimal foi encontrado no modelo. Use a medição por conjunto ou confira a numeração das peças."
                )

            # O TOTAL por componente continua sendo sequencial dentro de cada
            # conjunto. Ex.: se o maior subitem do conjunto 1 é 1.126, esse
            # conjunto possui 126 componentes para a medição, ainda que a LM
            # agrupe/omita posições intermediárias.
            for parent, child in expected_subitems:
                component_max_by_parent[parent] = max(
                    component_max_by_parent.get(parent, 0), int(child)
                )

            # As posições existentes no modelo podem ter saltos (ex.: o conjunto
            # 1 chega a 1.126, mas nem todos os números intermediários aparecem
            # como PART_POS distintos). A medição por componente precisa manter
            # o TOTAL sequencial solicitado (126), sem voltar ao erro da R40 de
            # mostrar apenas a quantidade de PART_POS distintos encontrados.
            #
            # Para isso a cobertura real das posições existentes no modelo é
            # projetada sobre o total sequencial do conjunto. Qualquer nova
            # posição encontrada em uma vista válida aumenta a cobertura, mesmo
            # que seu número seja menor que a maior posição já vista.
            component_expected_positions_by_parent: Dict[int, Set[int]] = {}
            for parent, child in expected_subitems:
                component_expected_positions_by_parent.setdefault(parent, set()).add(int(child))

            for parent, child in detected_subitems:
                maximum = component_max_by_parent.get(parent)
                if not maximum:
                    continue
                child = int(child)
                if child <= 0 or child > int(maximum):
                    continue
                if child not in component_expected_positions_by_parent.get(parent, set()):
                    continue
                component_detailed_by_parent.setdefault(parent, set()).add(child)
                detailed_subitems.add((parent, child))

            component_effective_by_parent: Dict[int, int] = {}
            for parent, maximum in component_max_by_parent.items():
                expected_children = component_expected_positions_by_parent.get(parent, set())
                detected_children = component_detailed_by_parent.get(parent, set())
                if not expected_children:
                    effective = 0
                elif len(detected_children) >= len(expected_children):
                    effective = int(maximum)
                else:
                    effective = int(round(int(maximum) * (len(detected_children) / len(expected_children))))
                component_effective_by_parent[parent] = max(0, min(int(maximum), effective))

            subitem_stats = dict(subitem_stats)
            subitem_stats.update(
                {
                    "component_max_by_parent": {
                        str(parent): int(maximum)
                        for parent, maximum in sorted(component_max_by_parent.items())
                    },
                    "component_expected_positions_by_parent": {
                        str(parent): numeric_sort(component_expected_positions_by_parent.get(parent, set()))
                        for parent in sorted(component_max_by_parent)
                    },
                    "component_detailed_by_parent": {
                        str(parent): numeric_sort(component_detailed_by_parent.get(parent, set()))
                        for parent in sorted(component_max_by_parent)
                    },
                    "component_effective_by_parent": {
                        str(parent): int(component_effective_by_parent.get(parent, 0))
                        for parent in sorted(component_max_by_parent)
                    },
                    "component_total_sequential": sum(component_max_by_parent.values()),
                    "component_detailed_actual_positions": len(detailed_subitems),
                    "component_detailed_effective": sum(component_effective_by_parent.values()),
                }
            )

            detail_views_used = int(drawing_stats.get("component_detail_views_used", 0) or 0)
            if detail_views_used:
                self._log(
                    f"Medição por componente: {detail_views_used} DetailView(s) usada(s) como evidência de detalhamento."
                )
            self._log(
                "Medição por componente individual: "
                + ", ".join(
                    f"conjunto {parent}: {len(component_detailed_by_parent.get(parent, set()))}/{maximum}"
                    for parent, maximum in sorted(component_max_by_parent.items())
                )
            )

        warnings: List[str] = []
        if mode == "subitem" and config.get("vistas_base_ignoradas"):
            ignored_count = int(drawing_stats.get("ignored_component_views", 0) or 0)
            matched_names = drawing_stats.get("ignored_component_view_names", []) or []
            if ignored_count:
                self._log(
                    "Lista de exceção aplicada no progresso por componente: "
                    + ", ".join(str(name) for name in matched_names)
                )
            else:
                warnings.append(
                    "Nenhuma entrada da lista de exceção foi reconhecida no desenho. "
                    "Informe o nome visível da vista ou corte, por exemplo A-A ou BL-BL."
                )
        if model_stats.get("duplicate_numbers"):
            warnings.append(
                f"{model_stats['duplicate_numbers']} numeração(ões) principal(is) repetida(s) no modelo."
            )
        drawing_name = identity["drawing_name"]
        performance = {
            "connect_seconds": round(connect_seconds, 3),
            "model_seconds": round(model_seconds, 3),
            "drawing_seconds": round(drawing_seconds, 3),
            "total_seconds": round(time.perf_counter() - total_start, 3),
        }
        tracking_detailed_items = [
            {
                "number": number,
                "tag": expected_map.get(number, ""),
                "pages": numeric_sort(item_pages.get(number, set())),
            }
            for number in numeric_sort(detailed)
        ]
        tracking_missing_items = [
            {"number": number, "tag": expected_map.get(number, "")}
            for number in numeric_sort(missing)
        ]

        if mode == "subitem":
            display_detailed_items = [
                {
                    "number": self.subitem_label(position),
                    "parent": int(position[0]),
                    "child": int(position[1]),
                    "tag": expected_subitems.get(position, ""),
                    # A folha é associada somente quando a própria posição
                    # apareceu explicitamente em uma marca de vista válida.
                    "pages": numeric_sort(subitem_pages.get(position, set())),
                }
                for position in sorted(detailed_subitems)
            ]
            # Não inventa uma lista 1.1..1.N de posições faltantes. O painel
            # mostra o saldo real por conjunto; o KPI usa a cobertura projetada.
            display_missing_items = []
            for parent, maximum in sorted(component_max_by_parent.items()):
                effective = int(component_effective_by_parent.get(parent, 0))
                remaining = max(0, int(maximum) - effective)
                if remaining:
                    display_missing_items.append(
                        {
                            "number": f"Conjunto {parent}",
                            "parent": int(parent),
                            "tag": f"{remaining} componentes faltando",
                        }
                    )
            metric_total = sum(component_max_by_parent.values())
            metric_detailed = sum(component_effective_by_parent.values())
            metric_missing = max(0, metric_total - metric_detailed)
        else:
            display_detailed_items = tracking_detailed_items
            display_missing_items = tracking_missing_items
            metric_total = len(expected)
            metric_detailed = len(detailed)
            metric_missing = len(missing)

        return {
            "project_key": identity["project_key"],
            "project_number": identity["project_number"],
            "model_path": identity["model_path"],
            "model_name": identity["model_name"],
            "ok": True,
            "drawing_name": drawing_name,
            "drawing_signature": identity.get("drawing_signature", ""),
            "progress_mode": mode,
            "total": metric_total,
            "detailed": metric_detailed,
            "missing": metric_missing,
            "detailed_items": display_detailed_items,
            "missing_items": display_missing_items,
            "tracking_detailed_items": tracking_detailed_items,
            "tracking_missing_items": tracking_missing_items,
            "detailed_numbers": numeric_sort(detailed),
            "missing_numbers": numeric_sort(missing),
            "tracking_detailed_numbers": numeric_sort(detailed),
            "tracking_missing_numbers": numeric_sort(missing),
            "marked_items": numeric_sort(marked & expected),
            "progress": round((metric_detailed / metric_total) * 100.0, 1) if metric_total else 0.0,
            "analyzed_at": now_time(),
            "model_stats": model_stats,
            "subitem_stats": subitem_stats,
            "component_max_by_parent": {
                str(parent): int(maximum)
                for parent, maximum in sorted(component_max_by_parent.items())
            },
            "component_detailed_by_parent": {
                str(parent): numeric_sort(component_detailed_by_parent.get(parent, set()))
                for parent in sorted(component_max_by_parent)
            },
            "component_effective_by_parent": {
                str(parent): int(component_effective_by_parent.get(parent, 0))
                for parent in sorted(component_max_by_parent)
            } if mode == "subitem" else {},
            # Mantido apenas para compatibilidade de leitura de versões anteriores.
            "component_frontier_by_parent": {
                str(parent): max(component_detailed_by_parent.get(parent, set()), default=0)
                for parent in sorted(component_max_by_parent)
            },
            "component_sets": [
                {
                    "number": int(parent),
                    "tag": "",
                    "detailed": int(component_effective_by_parent.get(parent, 0)),
                    "total": int(maximum),
                    "progress": round(
                        (component_effective_by_parent.get(parent, 0) / maximum) * 100.0, 1
                    ) if maximum else 0.0,
                }
                for parent, maximum in sorted(component_max_by_parent.items())
            ] if mode == "subitem" else [],
            "component_descriptions": {
                self.subitem_label(position): clean_text(description)
                for position, description in sorted(expected_subitems.items())
                if clean_text(description)
            } if mode == "subitem" else {},
            "subitem_views": {
                self.subitem_label(position): sorted(views)
                for position, views in sorted(subitem_views.items())
            } if mode == "subitem" else {},
            "drawing_stats": drawing_stats,
            "performance": performance,
            "cota_pending": True,
            "cota_summary": self.empty_cota_summary("pending"),
            "cota_items": [],
            "item_views": {str(number): sorted(views) for number, views in item_views.items()},
            "item_pages": {
                str(number): numeric_sort(pages) for number, pages in item_pages.items()
            },
            "ignored_detail_items": numeric_sort(
                drawing_stats.get("ignored_detail_items", [])
            ),
            "warnings": warnings,
        }



class DetailHistory:
    """Histórico persistente de início, término e tempo por item e por projeto."""

    def __init__(self, path: Path):
        self.path = path
        self.runtime_id = uuid.uuid4().hex
        self.data: Dict[str, Any] = {"version": 2, "projects": {}}
        self._load()

    def _previous_history_candidates(self) -> List[Path]:
        candidates: List[Path] = []
        try:
            for folder in self.path.parent.parent.glob("PROGRESSO DE DETALHAMENTO.v.*"):
                candidate = folder / self.path.name
                if candidate != self.path and candidate.is_file():
                    candidates.append(candidate)
        except Exception:
            pass
        try:
            for folder in self.path.parent.glob("PROGRESSO DE DETALHAMENTO.v.*"):
                candidate = folder / self.path.name
                if candidate != self.path and candidate.is_file():
                    candidates.append(candidate)
        except Exception:
            pass
        unique = {str(candidate.resolve()): candidate for candidate in candidates}
        return sorted(
            unique.values(),
            key=lambda candidate: candidate.stat().st_mtime if candidate.exists() else 0,
            reverse=True,
        )

    @staticmethod
    def _latest_text(*values: Any) -> str:
        valid = [clean_text(value) for value in values if clean_text(value)]
        return max(valid) if valid else ""

    @staticmethod
    def _earliest_text(*values: Any) -> str:
        valid = [clean_text(value) for value in values if clean_text(value)]
        return min(valid) if valid else ""

    @staticmethod
    def _session_key(session: Dict[str, Any]) -> str:
        try:
            return json.dumps(session, ensure_ascii=False, sort_keys=True, default=str)
        except Exception:
            return repr(session)

    @staticmethod
    def _positive_int(value: Any) -> int:
        try:
            number = int(float(clean_text(value).replace(",", ".")))
            return number if number > 0 else 0
        except Exception:
            return 0

    @classmethod
    def _sheet_total_from_result(cls, result: Dict[str, Any]) -> int:
        """Maior folha que ja tem item detalhado na varredura atual.

        ``temporary_pages`` registra margens criadas no multidesenho. Para o
        Controle de Projeto, antes do PDF final, a folha so conta quando algum
        item detalhado foi associado a ela.
        """
        highest = 0

        def bump(value: Any) -> None:
            nonlocal highest
            number = cls._positive_int(value)
            if number > highest:
                highest = number

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

        for item in result.get("detailed_items", []) or []:
            if isinstance(item, dict):
                consume(item.get("pages", []))
        item_pages = result.get("item_pages", {}) or {}
        if isinstance(item_pages, dict):
            for pages in item_pages.values():
                consume(pages)
        return highest

    def _merge_item_record(
        self, current: Any, incoming: Any
    ) -> Dict[str, Any]:
        if not isinstance(current, dict):
            return dict(incoming) if isinstance(incoming, dict) else {}
        if not isinstance(incoming, dict):
            return current

        merged = dict(current)
        for field in ("number", "tag"):
            value = clean_text(incoming.get(field))
            if value and not clean_text(merged.get(field)):
                merged[field] = incoming.get(field)

        detected_at = self._earliest_text(
            merged.get("detected_at"), incoming.get("detected_at")
        )
        finished_at = self._latest_text(
            merged.get("finished_at"), incoming.get("finished_at")
        )
        merged["detected_at"] = detected_at
        merged["finished_at"] = finished_at
        merged["tracked_seconds"] = round(
            self._safe_seconds(merged.get("tracked_seconds"))
            + self._safe_seconds(incoming.get("tracked_seconds")),
            3,
        )
        merged["historical"] = bool(
            merged.get("historical", False) and incoming.get("historical", False)
        )
        merged["pages"] = numeric_sort(
            list(merged.get("pages", []) or []) + list(incoming.get("pages", []) or [])
        )

        sessions: List[Dict[str, Any]] = []
        seen: Set[str] = set()
        for session in list(merged.get("sessions", []) or []) + list(
            incoming.get("sessions", []) or []
        ):
            if not isinstance(session, dict):
                continue
            key = self._session_key(session)
            if key in seen:
                continue
            seen.add(key)
            sessions.append(dict(session))
        sessions.sort(
            key=lambda session: (
                clean_text(session.get("started_at")),
                clean_text(session.get("finished_at")),
            )
        )
        merged["sessions"] = sessions[-50:]
        return merged

    def _merge_project_data(
        self, current: Dict[str, Any], incoming: Dict[str, Any], project_key: str
    ) -> Dict[str, Any]:
        merged = dict(current) if isinstance(current, dict) else {}
        incoming = incoming if isinstance(incoming, dict) else {}
        incoming_newer = (
            self._latest_text(
                merged.get("last_analysis_at"), incoming.get("last_analysis_at")
            )
            == clean_text(incoming.get("last_analysis_at"))
        )

        merged["project_key"] = project_key
        for field in ("project_number", "model_path", "model_name", "drawing_name"):
            incoming_value = clean_text(incoming.get(field))
            if incoming_value and (incoming_newer or not clean_text(merged.get(field))):
                merged[field] = incoming_value

        created_at = self._earliest_text(merged.get("created_at"), incoming.get("created_at"))
        if created_at:
            merged["created_at"] = created_at
        for field in ("last_tick_at", "last_analysis_at"):
            latest = self._latest_text(merged.get(field), incoming.get(field))
            if latest:
                merged[field] = latest

        # Estes campos formam o retrato oficial do avanço da etapa Detalhamento.
        # Eles precisam acompanhar o registro mais recente quando históricos/cache
        # são unidos; sem isso, a mini interface exibe o percentual correto, mas o
        # Controle de Projeto não recebe o total e a quantidade detalhada.
        for field in (
            "last_scan_total",
            "last_scan_detailed_count",
            "last_scan_missing_count",
            "last_scan_progress",
        ):
            incoming_has_value = field in incoming and incoming.get(field) is not None
            current_has_value = field in merged and merged.get(field) is not None
            if incoming_has_value and (incoming_newer or not current_has_value):
                merged[field] = incoming.get(field)

        for field in ("last_scan_sheet_total", "sheet_total"):
            incoming_has_value = field in incoming and incoming.get(field) is not None
            current_has_value = field in merged and merged.get(field) is not None
            if incoming_has_value and (incoming_newer or not current_has_value):
                merged[field] = self._positive_int(incoming.get(field))
            elif current_has_value:
                merged[field] = self._positive_int(merged.get(field))

        known = set()
        for source in (merged.get("known_detailed", []), incoming.get("known_detailed", [])):
            for value in source or []:
                try:
                    if int(value) > 0:
                        known.add(int(value))
                except Exception:
                    continue
        merged["known_detailed"] = numeric_sort(known)
        last_scan_source = (
            incoming.get("last_scan_detailed", [])
            if incoming_newer
            else merged.get("last_scan_detailed", [])
        )
        last_scan = set()
        for value in last_scan_source or []:
            try:
                if int(value) > 0:
                    last_scan.add(int(value))
            except Exception:
                continue
        if last_scan:
            merged["last_scan_detailed"] = numeric_sort(last_scan)

        if not isinstance(merged.get("items"), dict):
            merged["items"] = {}
        items = merged["items"]
        for key, record in (incoming.get("items", {}) or {}).items():
            if not isinstance(record, dict):
                continue
            try:
                item_key = str(int(record.get("number", key)))
            except Exception:
                item_key = clean_text(key)
            if not item_key:
                continue
            items[item_key] = self._merge_item_record(items.get(item_key), record)

        current_active = merged.get("active_session")
        incoming_active = incoming.get("active_session")
        current_has_active = isinstance(current_active, dict) and bool(
            current_active.get("items")
        )
        incoming_has_active = isinstance(incoming_active, dict) and bool(
            incoming_active.get("items")
        )
        if incoming_has_active:
            current_time = clean_text(current_active.get("started_at")) if current_has_active else ""
            incoming_time = clean_text(incoming_active.get("started_at"))
            if not current_has_active or self._latest_text(current_time, incoming_time) == incoming_time:
                merged["active_session"] = dict(incoming_active)
        elif incoming_newer and "active_session" in incoming:
            merged["active_session"] = None
        elif "active_session" not in merged:
            merged["active_session"] = None

        runtime_id = (
            clean_text(incoming.get("runtime_id"))
            if incoming_newer
            else clean_text(merged.get("runtime_id"))
        )
        if runtime_id:
            merged["runtime_id"] = runtime_id
        return merged

    def _normalize_project_keys(self) -> bool:
        projects = self.data.get("projects", {})
        if not isinstance(projects, dict):
            self.data["projects"] = {}
            return True

        normalized: Dict[str, Dict[str, Any]] = {}
        changed = False
        for original_key, project in projects.items():
            if not isinstance(project, dict):
                changed = True
                continue
            canonical_key = project_identity_key(
                project.get("project_number"),
                project.get("drawing_name"),
                project.get("model_name"),
                project.get("model_path"),
            )
            if not canonical_key:
                canonical_key = clean_text(original_key)
            if canonical_key != clean_text(original_key):
                changed = True

            project_copy = dict(project)
            project_copy["project_key"] = canonical_key
            if canonical_key in normalized:
                normalized[canonical_key] = self._merge_project_data(
                    normalized[canonical_key], project_copy, canonical_key
                )
                changed = True
            else:
                normalized[canonical_key] = project_copy

        if changed:
            self.data["projects"] = normalized
        return changed

    def _load(self) -> None:
        source = self.path if self.path.is_file() else None
        if source is None:
            previous = self._previous_history_candidates()
            source = previous[0] if previous else None
        try:
            if source is not None:
                loaded = json.loads(source.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self.data.update(loaded)
            if not isinstance(self.data.get("projects"), dict):
                self.data["projects"] = {}
            self.data["version"] = 2
            changed = self._normalize_project_keys()
            if source is not None and (source != self.path or changed):
                self._save()
        except Exception:
            self.data = {"version": 2, "projects": {}}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temp.replace(self.path)

    @staticmethod
    def _safe_seconds(value: Any) -> float:
        try:
            return max(0.0, float(value or 0.0))
        except Exception:
            return 0.0


    @staticmethod
    def _component_label(value: Any) -> str:
        parsed = parse_subitem_position(value)
        if not parsed:
            return ""
        return f"{int(parsed[0])}.{int(parsed[1])}"

    @staticmethod
    def _component_sort_key(value: Any) -> Tuple[int, int]:
        parsed = parse_subitem_position(value)
        return parsed if parsed else (0, 0)

    def _component_record(
        self,
        project: Dict[str, Any],
        label: str,
        description: str = "",
    ) -> Dict[str, Any]:
        normalized = self._component_label(label)
        if not normalized:
            return {}
        parent, child = self._component_sort_key(normalized)
        records = project.setdefault("component_items", {})
        record = records.get(normalized)
        if not isinstance(record, dict):
            record = {
                "number": normalized,
                "parent": int(parent),
                "child": int(child),
                "description": clean_text(description),
                "detected_at": "",
                "finished_at": "",
                "tracked_seconds": 0.0,
                "sessions": [],
            }
            records[normalized] = record
        elif description:
            record["description"] = clean_text(description)
        return record

    def _component_active_is_live(self, project: Dict[str, Any]) -> bool:
        active = project.get("component_active_session")
        return (
            isinstance(active, dict)
            and bool(active.get("items"))
            and clean_text(active.get("runtime_id")) == self.runtime_id
        )

    def _accrue_component_active(
        self, project: Dict[str, Any], now_dt: datetime, interval: int
    ) -> None:
        active = project.get("component_active_session")
        if not isinstance(active, dict) or not active.get("items"):
            return
        if not self._component_active_is_live(project):
            return
        previous = parse_iso(project.get("component_last_tick_at"))
        if previous is None:
            return
        try:
            delta = max(0.0, (now_dt - previous).total_seconds())
        except Exception:
            return
        cap = max(90.0, float(max(10, interval)) * 1.75)
        active["elapsed_seconds"] = self._safe_seconds(
            active.get("elapsed_seconds")
        ) + min(delta, cap)

    def _finish_component_active(
        self, project: Dict[str, Any], finished_at: str
    ) -> None:
        active = project.get("component_active_session")
        if not isinstance(active, dict):
            project["component_active_session"] = None
            return
        labels = sorted(
            {
                self._component_label(value)
                for value in active.get("items", []) or []
                if self._component_label(value)
            },
            key=self._component_sort_key,
        )
        if not labels:
            project["component_active_session"] = None
            return
        elapsed = self._safe_seconds(active.get("elapsed_seconds"))
        share = elapsed / len(labels) if labels else 0.0
        for label in labels:
            record = self._component_record(project, label)
            if not record:
                continue
            if not clean_text(record.get("detected_at")):
                record["detected_at"] = clean_text(active.get("started_at"))
            record["tracked_seconds"] = self._safe_seconds(
                record.get("tracked_seconds")
            ) + share
            record["finished_at"] = finished_at
            sessions = record.setdefault("sessions", [])
            sessions.append(
                {
                    "started_at": clean_text(active.get("started_at")),
                    "finished_at": finished_at,
                    "group_items": labels,
                    "view_keys": list(active.get("view_keys", []) or []),
                    "allocated_seconds": round(share, 3),
                    "group_seconds": round(elapsed, 3),
                }
            )
            record["sessions"] = sessions[-80:]
        project["component_active_session"] = None

    def _start_component_active(
        self,
        project: Dict[str, Any],
        labels: Iterable[str],
        view_keys: Iterable[str],
        started_at: str,
        descriptions: Dict[str, str],
    ) -> None:
        clean_labels = sorted(
            {
                self._component_label(value)
                for value in labels
                if self._component_label(value)
            },
            key=self._component_sort_key,
        )
        if not clean_labels:
            return
        for label in clean_labels:
            record = self._component_record(
                project, label, descriptions.get(label, "")
            )
            if record:
                if not clean_text(record.get("detected_at")):
                    record["detected_at"] = started_at
                record["finished_at"] = ""
        project["component_active_session"] = {
            "items": clean_labels,
            "view_keys": sorted(
                {clean_text(value) for value in view_keys if clean_text(value)}
            ),
            "started_at": started_at,
            "elapsed_seconds": 0.0,
            "runtime_id": self.runtime_id,
        }

    def _merge_component_active(
        self,
        project: Dict[str, Any],
        labels: Iterable[str],
        view_keys: Iterable[str],
        detected_at: str,
        descriptions: Dict[str, str],
    ) -> None:
        active = project.get("component_active_session")
        if not isinstance(active, dict):
            self._start_component_active(
                project, labels, view_keys, detected_at, descriptions
            )
            return
        merged = {
            self._component_label(value)
            for value in list(active.get("items", [])) + list(labels)
            if self._component_label(value)
        }
        active["items"] = sorted(merged, key=self._component_sort_key)
        active["view_keys"] = sorted(
            set(active.get("view_keys", []))
            | {clean_text(value) for value in view_keys if clean_text(value)}
        )
        active["runtime_id"] = self.runtime_id
        for label in labels:
            normalized = self._component_label(label)
            if not normalized:
                continue
            record = self._component_record(
                project, normalized, descriptions.get(normalized, "")
            )
            if record and not clean_text(record.get("detected_at")):
                record["detected_at"] = detected_at

    def _prune_component_state(
        self, project: Dict[str, Any], current_labels: Set[str]
    ) -> None:
        """Remove componentes que a leitura atual nao considera detalhados."""
        normalized_current = {
            self._component_label(value)
            for value in current_labels
            if self._component_label(value)
        }

        records = project.get("component_items")
        if isinstance(records, dict):
            for label in list(records.keys()):
                normalized = self._component_label(label)
                if normalized and normalized not in normalized_current:
                    records.pop(label, None)

        active = project.get("component_active_session")
        if not isinstance(active, dict):
            return
        active_labels = {
            self._component_label(value)
            for value in active.get("items", []) or []
            if self._component_label(value)
        }
        kept = active_labels & normalized_current
        if kept:
            active["items"] = sorted(kept, key=self._component_sort_key)
        else:
            project["component_active_session"] = None

    def _migrate_component_time_events(self, project: Dict[str, Any]) -> None:
        """Aproveita o histórico da R41 sem inventar tempo anterior à primeira base.

        Cada grupo já detectado depois da base recebe o intervalo até o próximo
        levantamento. O intervalo é dividido igualmente entre os componentes
        daquele grupo, como solicitado para cortes/detalhes com vários subitens.
        """
        if int(project.get("component_time_schema") or 0) >= 1:
            return
        events = [
            event for event in (project.get("component_metric_events", []) or [])
            if isinstance(event, dict) and parse_iso(event.get("detected_at")) is not None
        ]
        events.sort(key=lambda event: parse_iso(event.get("detected_at")))
        last_analysis = parse_iso(project.get("last_analysis_at"))
        for index, event in enumerate(events):
            if bool(event.get("initial")):
                continue
            start = parse_iso(event.get("detected_at"))
            if start is None:
                continue
            finish = None
            if index + 1 < len(events):
                finish = parse_iso(events[index + 1].get("detected_at"))
            if finish is None:
                finish = last_analysis
            if finish is None or finish <= start:
                continue
            elapsed = max(0.0, (finish - start).total_seconds())
            # Evita importar períodos absurdos de programa parado.
            if elapsed <= 0 or elapsed > 4 * 3600:
                continue
            labels = sorted(
                {
                    self._component_label(value)
                    for value in event.get("numbers", []) or []
                    if self._component_label(value)
                },
                key=self._component_sort_key,
            )
            if not labels:
                continue
            share = elapsed / len(labels)
            for label in labels:
                record = self._component_record(project, label)
                if not record:
                    continue
                if not clean_text(record.get("detected_at")):
                    record["detected_at"] = start.isoformat(timespec="milliseconds")
                record["tracked_seconds"] = self._safe_seconds(
                    record.get("tracked_seconds")
                ) + share
                record["finished_at"] = finish.isoformat(timespec="milliseconds")
                sessions = record.setdefault("sessions", [])
                sessions.append(
                    {
                        "started_at": start.isoformat(timespec="milliseconds"),
                        "finished_at": finish.isoformat(timespec="milliseconds"),
                        "group_items": labels,
                        "view_keys": [],
                        "allocated_seconds": round(share, 3),
                        "group_seconds": round(elapsed, 3),
                        "migrated": True,
                    }
                )
                record["sessions"] = sessions[-80:]
        project["component_time_schema"] = 1

    def _apply_multisheet_component_progress(
        self, project: Dict[str, Any], result: Dict[str, Any]
    ) -> None:
        """Soma o progresso por componente de TODAS as folhas (desenhos) do projeto.

        A leitura ao vivo enxerga apenas a folha (desenho) ativa. Um componente
        detalhado em OUTRA folha do mesmo projeto continua fazendo parte do
        detalhamento e não pode sumir só porque a folha ativa mudou. Por isso
        guardamos, por folha, o último resultado lido dela, e o progresso do
        projeto é a UNIÃO dessas folhas.

        Isto NÃO é "nunca reduzir": ao reanalisar a MESMA folha, a contribuição
        dela é SUBSTITUÍDA pela leitura nova. Assim a lista de exceção (vistas-base
        ignoradas) e a retirada de um detalhe continuam reduzindo normalmente o
        que pertence àquela folha — apenas não zeram o que está nas outras folhas.
        """
        if clean_text(result.get("progress_mode")) != "subitem":
            return

        maxima: Dict[int, int] = {}
        for key, value in (result.get("component_max_by_parent", {}) or {}).items():
            try:
                parent = int(key)
                maximum = max(0, int(value or 0))
            except Exception:
                continue
            if maximum > 0:
                maxima[parent] = maximum
        if not maxima:
            return

        expected_positions: Dict[int, Set[int]] = {}
        stats = result.get("subitem_stats", {}) or {}
        for key, values in (stats.get("component_expected_positions_by_parent", {}) or {}).items():
            try:
                parent = int(key)
            except Exception:
                continue
            maximum = maxima.get(parent, 0)
            children: Set[int] = set()
            for raw_child in values or []:
                try:
                    child = int(raw_child)
                except Exception:
                    continue
                if 0 < child <= maximum:
                    children.add(child)
            expected_positions[parent] = children

        def _read_detected(raw: Any) -> Dict[int, Set[int]]:
            out: Dict[int, Set[int]] = {}
            if not isinstance(raw, dict):
                return out
            for key, values in raw.items():
                try:
                    parent = int(key)
                except Exception:
                    continue
                maximum = maxima.get(parent, 0)
                if maximum <= 0:
                    continue
                children: Set[int] = set()
                for raw_child in values or []:
                    try:
                        child = int(raw_child)
                    except Exception:
                        continue
                    if 0 < child <= maximum:
                        children.add(child)
                if children:
                    out[parent] = children
            return out

        incoming = _read_detected(result.get("component_detailed_by_parent"))

        # Identidade estável da folha (desenho) ativa.
        sheet_key = (
            clean_text(result.get("drawing_signature"))
            or clean_text(result.get("drawing_name"))
            or "__folha_unica__"
        )

        sheets = project.get("component_detected_by_sheet")
        if not isinstance(sheets, dict):
            sheets = {}
            project["component_detected_by_sheet"] = sheets

        # Substitui a contribuição da folha ATUAL pela leitura nova. É isto que
        # mantém a lista de exceção funcionando: se um componente deixou de ser
        # detectado NESTA folha (p.ex. a vista virou vista-base ignorada), ele sai
        # da contribuição dela. Apenas as OUTRAS folhas ficam preservadas.
        if incoming:
            sheets[sheet_key] = {
                str(parent): numeric_sort(children)
                for parent, children in incoming.items()
            }
        else:
            sheets.pop(sheet_key, None)

        # União do último resultado conhecido de cada folha, restrita ao que
        # ainda é válido no modelo (auto-corrige numerações que sumiram).
        merged: Dict[int, Set[int]] = {}
        for sheet_map in sheets.values():
            if not isinstance(sheet_map, dict):
                continue
            for key, values in sheet_map.items():
                try:
                    parent = int(key)
                except Exception:
                    continue
                maximum = maxima.get(parent, 0)
                if maximum <= 0:
                    continue
                allowed = expected_positions.get(parent)
                for raw_child in values or []:
                    try:
                        child = int(raw_child)
                    except Exception:
                        continue
                    if child <= 0 or child > maximum:
                        continue
                    if allowed and child not in allowed:
                        continue
                    merged.setdefault(parent, set()).add(child)

        # Cobertura efetiva: mesma projeção usada na medição ao vivo
        # (posições detectadas / posições esperadas, aplicada ao total sequencial).
        effective: Dict[int, int] = {}
        for parent, maximum in maxima.items():
            expected_children = expected_positions.get(parent, set())
            detected_children = merged.get(parent, set())
            if not expected_children:
                value = 0
            elif len(detected_children) >= len(expected_children):
                value = int(maximum)
            else:
                value = int(round(int(maximum) * (len(detected_children) / len(expected_children))))
            effective[parent] = max(0, min(int(maximum), value))

        metric_total = sum(maxima.values())
        metric_detailed = sum(effective.values())
        metric_missing = max(0, metric_total - metric_detailed)

        descriptions = result.get("component_descriptions", {}) or {}
        live_pages: Dict[str, List[int]] = {}
        for item in result.get("detailed_items", []) or []:
            if isinstance(item, dict):
                label = clean_text(item.get("number"))
                if label:
                    live_pages[label] = item.get("pages", []) or []

        detailed_items: List[Dict[str, Any]] = []
        for parent in sorted(merged):
            for child in sorted(merged[parent]):
                label = f"{parent}.{child}"
                detailed_items.append(
                    {
                        "number": label,
                        "parent": int(parent),
                        "child": int(child),
                        "tag": clean_text(descriptions.get(label, "")),
                        # Só há folha associada quando a posição apareceu na leitura
                        # atual; posições de outras folhas ficam sem página aqui.
                        "pages": live_pages.get(label, []),
                    }
                )

        missing_items: List[Dict[str, Any]] = []
        for parent in sorted(maxima):
            remaining = max(0, int(maxima[parent]) - int(effective.get(parent, 0)))
            if remaining:
                missing_items.append(
                    {
                        "number": f"Conjunto {parent}",
                        "parent": int(parent),
                        "tag": f"{remaining} componentes faltando",
                    }
                )

        result["component_detailed_by_parent"] = {
            str(parent): numeric_sort(children) for parent, children in sorted(merged.items())
        }
        result["component_effective_by_parent"] = {
            str(parent): int(effective.get(parent, 0)) for parent in sorted(maxima)
        }
        result["component_frontier_by_parent"] = {
            str(parent): max(merged.get(parent, set()), default=0) for parent in sorted(maxima)
        }
        result["component_sets"] = [
            {
                "number": int(parent),
                "tag": "",
                "detailed": int(effective.get(parent, 0)),
                "total": int(maxima[parent]),
                "progress": round((effective.get(parent, 0) / maxima[parent]) * 100.0, 1)
                if maxima[parent]
                else 0.0,
            }
            for parent in sorted(maxima)
        ]
        result["detailed_items"] = detailed_items
        result["missing_items"] = missing_items
        result["total"] = metric_total
        result["detailed"] = metric_detailed
        result["missing"] = metric_missing
        result["progress"] = (
            round((metric_detailed / metric_total) * 100.0, 1) if metric_total else 0.0
        )
        if isinstance(result.get("subitem_stats"), dict):
            result["subitem_stats"]["component_detailed_by_parent"] = dict(
                result["component_detailed_by_parent"]
            )
            result["subitem_stats"]["component_effective_by_parent"] = dict(
                result["component_effective_by_parent"]
            )
            result["subitem_stats"]["component_detailed_actual_positions"] = sum(
                len(children) for children in merged.values()
            )
            result["subitem_stats"]["component_detailed_effective"] = metric_detailed

    def _update_component_timing(
        self,
        project: Dict[str, Any],
        result: Dict[str, Any],
        timestamp: str,
        now_dt: datetime,
        interval: int,
    ) -> None:
        self._migrate_component_time_events(project)
        descriptions = {
            self._component_label(key): clean_text(value)
            for key, value in (result.get("component_descriptions", {}) or {}).items()
            if self._component_label(key)
        }
        current_labels: Set[str] = set()
        for parent, children in (result.get("component_detailed_by_parent", {}) or {}).items():
            try:
                parent_number = int(parent)
            except Exception:
                continue
            for child in children or []:
                try:
                    label = f"{parent_number}.{int(child)}"
                except Exception:
                    continue
                current_labels.add(label)
                self._component_record(project, label, descriptions.get(label, ""))

        self._prune_component_state(project, current_labels)

        previous_labels: Set[str] = set()
        previous_map = project.get("component_detected_by_parent", {}) or {}
        if isinstance(previous_map, dict):
            for parent, children in previous_map.items():
                try:
                    parent_number = int(parent)
                except Exception:
                    continue
                for child in children or []:
                    try:
                        previous_labels.add(f"{parent_number}.{int(child)}")
                    except Exception:
                        continue

        self._accrue_component_active(project, now_dt, interval)
        new_labels = current_labels - previous_labels
        if new_labels:
            views_raw = result.get("subitem_views", {}) or {}
            new_views: Set[str] = set()
            for label in new_labels:
                for value in views_raw.get(label, []) or []:
                    value = clean_text(value)
                    if value:
                        new_views.add(value)

            active = project.get("component_active_session")
            active_views = (
                {
                    clean_text(value)
                    for value in active.get("view_keys", []) or []
                    if clean_text(value)
                }
                if isinstance(active, dict)
                else set()
            )
            same_cut = bool(
                self._component_active_is_live(project)
                and active_views
                and new_views
                and active_views.intersection(new_views)
            )
            if isinstance(active, dict) and active.get("items") and same_cut:
                self._merge_component_active(
                    project, new_labels, new_views, timestamp, descriptions
                )
            else:
                if isinstance(active, dict) and active.get("items"):
                    finished_at = (
                        timestamp
                        if self._component_active_is_live(project)
                        else clean_text(project.get("component_last_tick_at")) or timestamp
                    )
                    self._finish_component_active(project, finished_at)
                self._start_component_active(
                    project, new_labels, new_views, timestamp, descriptions
                )

        project["component_last_tick_at"] = timestamp

    def _component_time_summaries(
        self, project: Dict[str, Any], project_key: str, current_project_key: str
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        records = project.get("component_items", {}) or {}
        active = project.get("component_active_session")
        active_labels = (
            {
                self._component_label(value)
                for value in active.get("items", []) or []
                if self._component_label(value)
            }
            if isinstance(active, dict)
            else set()
        )
        active_elapsed = (
            self._safe_seconds(active.get("elapsed_seconds"))
            if isinstance(active, dict)
            else 0.0
        )
        active_live = bool(
            project_key == current_project_key and self._component_active_is_live(project)
        )
        active_share = active_elapsed / len(active_labels) if active_labels else 0.0

        component_rows: List[Dict[str, Any]] = []
        set_seconds: Dict[int, float] = {}
        set_started: Dict[int, str] = {}
        set_active: Set[int] = set()

        for label, raw_record in records.items():
            if not isinstance(raw_record, dict):
                continue
            normalized = self._component_label(label) or self._component_label(raw_record.get("number"))
            if not normalized:
                continue
            parent, child = self._component_sort_key(normalized)
            in_active_group = normalized in active_labels
            is_active = bool(in_active_group and active_live)
            seconds = self._safe_seconds(raw_record.get("tracked_seconds"))
            if in_active_group:
                seconds += active_share
            if is_active:
                set_active.add(int(parent))
            started = clean_text(raw_record.get("detected_at"))
            if started and (int(parent) not in set_started or started < set_started[int(parent)]):
                set_started[int(parent)] = started
            set_seconds[int(parent)] = set_seconds.get(int(parent), 0.0) + seconds
            component_rows.append(
                {
                    "number": normalized,
                    "parent": int(parent),
                    "child": int(child),
                    "description": clean_text(raw_record.get("description")),
                    "seconds": round(seconds, 1),
                    "active": bool(is_active),
                }
            )

        # Usa o início original do conjunto quando ele já existe no histórico.
        parent_records = project.get("items", {}) or {}
        for parent in set_seconds:
            parent_record = parent_records.get(str(parent))
            if not isinstance(parent_record, dict):
                continue
            parent_start = clean_text(parent_record.get("detected_at"))
            if parent_start and (
                parent not in set_started or parent_start < set_started[parent]
            ):
                set_started[parent] = parent_start

        component_rows.sort(key=lambda entry: self._component_sort_key(entry.get("number")))
        set_rows = [
            {
                "number": parent,
                "seconds": round(seconds, 1),
                "started_at": set_started.get(parent, ""),
                "active": parent in set_active,
            }
            for parent, seconds in sorted(set_seconds.items())
            if seconds > 0 or parent in set_active
        ]
        return set_rows, component_rows

    @staticmethod
    def _tag_map(result: Dict[str, Any]) -> Dict[int, str]:
        tags: Dict[int, str] = {}
        for key in ("tracking_detailed_items", "tracking_missing_items", "detailed_items", "missing_items"):
            for entry in result.get(key, []) or []:
                try:
                    tags[int(entry.get("number"))] = clean_text(entry.get("tag"))
                except Exception:
                    continue
        return tags

    @staticmethod
    def _project_label(project: Dict[str, Any]) -> str:
        return (
            clean_text(project.get("project_number"))
            or clean_text(project.get("drawing_name"))
            or clean_text(project.get("model_name"))
            or "Projeto sem identificação"
        )

    def _accrue_active(
        self, project: Dict[str, Any], now_dt: datetime, interval: int
    ) -> None:
        active = project.get("active_session")
        if not isinstance(active, dict) or not active.get("items"):
            return
        if not self._active_is_live(project):
            return
        previous = parse_iso(project.get("last_tick_at"))
        if previous is None:
            return
        try:
            delta = max(0.0, (now_dt - previous).total_seconds())
        except Exception:
            return
        # Evita contabilizar horas de suspensão, falha de conexão ou programa parado.
        cap = max(90.0, float(max(10, interval)) * 1.75)
        active["elapsed_seconds"] = self._safe_seconds(
            active.get("elapsed_seconds")
        ) + min(delta, cap)

    def _active_runtime_id(self, project: Dict[str, Any]) -> str:
        active = project.get("active_session")
        if not isinstance(active, dict):
            return ""
        return clean_text(active.get("runtime_id")) or clean_text(project.get("runtime_id"))

    def _active_is_live(self, project: Dict[str, Any]) -> bool:
        active = project.get("active_session")
        return (
            isinstance(active, dict)
            and bool(active.get("items"))
            and self._active_runtime_id(project) == self.runtime_id
        )

    def _stamp_active_runtime(self, project: Dict[str, Any]) -> None:
        active = project.get("active_session")
        if not isinstance(active, dict) or clean_text(active.get("runtime_id")):
            return
        runtime_id = clean_text(project.get("runtime_id"))
        if runtime_id:
            active["runtime_id"] = runtime_id

    def _reconcile_active_with_current_detail(
        self, project: Dict[str, Any], detailed: Set[int]
    ) -> Set[int]:
        """Cancela itens ativos que deixaram de existir na leitura atual."""
        active = project.get("active_session")
        if not isinstance(active, dict) or not active.get("items"):
            return set()
        active_numbers = {
            int(value)
            for value in active.get("items", [])
            if str(value).isdigit() and int(value) > 0
        }
        stale = active_numbers - detailed
        if not stale:
            return set()
        remaining = active_numbers & detailed
        if remaining:
            active["items"] = numeric_sort(remaining)
        else:
            project["active_session"] = None
        return stale

    def _item_record(
        self,
        project: Dict[str, Any],
        number: int,
        tag: str,
        historical: bool = False,
        detected_at: str = "",
    ) -> Dict[str, Any]:
        items = project.setdefault("items", {})
        key = str(int(number))
        record = items.get(key)
        if not isinstance(record, dict):
            record = {
                "number": int(number),
                "tag": tag,
                "detected_at": detected_at,
                "finished_at": "",
                "tracked_seconds": 0.0,
                "historical": bool(historical),
                "pages": [],
                "sessions": [],
            }
            items[key] = record
        elif tag:
            record["tag"] = tag
        return record

    def _finish_active(self, project: Dict[str, Any], finished_at: str) -> None:
        active = project.get("active_session")
        if not isinstance(active, dict):
            project["active_session"] = None
            return
        numbers = numeric_sort(active.get("items", []))
        if not numbers:
            project["active_session"] = None
            return
        elapsed = self._safe_seconds(active.get("elapsed_seconds"))
        share = elapsed / len(numbers) if numbers else 0.0
        for number in numbers:
            record = self._item_record(project, number, "")
            record["tracked_seconds"] = self._safe_seconds(
                record.get("tracked_seconds")
            ) + share
            record["finished_at"] = finished_at
            record["historical"] = False
            sessions = record.setdefault("sessions", [])
            sessions.append(
                {
                    "started_at": clean_text(active.get("started_at")),
                    "finished_at": finished_at,
                    "group_items": numbers,
                    "allocated_seconds": round(share, 3),
                }
            )
            record["sessions"] = sessions[-50:]
        project["active_session"] = None

    def _start_active(
        self,
        project: Dict[str, Any],
        numbers: Iterable[int],
        view_keys: Iterable[str],
        started_at: str,
        tags: Dict[int, str],
    ) -> None:
        clean_numbers = numeric_sort(numbers)
        if not clean_numbers:
            return
        for number in clean_numbers:
            record = self._item_record(
                project,
                number,
                tags.get(number, ""),
                historical=False,
                detected_at=started_at,
            )
            record["detected_at"] = started_at
            record["finished_at"] = ""
            record["historical"] = False
        project["active_session"] = {
            "items": clean_numbers,
            "view_keys": sorted(
                {clean_text(value) for value in view_keys if clean_text(value)}
            ),
            "started_at": started_at,
            "elapsed_seconds": 0.0,
            "runtime_id": self.runtime_id,
        }

    def _merge_active(
        self,
        project: Dict[str, Any],
        numbers: Iterable[int],
        view_keys: Iterable[str],
        detected_at: str,
        tags: Dict[int, str],
    ) -> None:
        active = project.get("active_session")
        if not isinstance(active, dict):
            self._start_active(project, numbers, view_keys, detected_at, tags)
            return
        merged = numeric_sort(list(active.get("items", [])) + list(numbers))
        active["items"] = merged
        active["view_keys"] = sorted(
            set(active.get("view_keys", []))
            | {clean_text(value) for value in view_keys if clean_text(value)}
        )
        active["runtime_id"] = self.runtime_id
        for number in numbers:
            record = self._item_record(
                project,
                int(number),
                tags.get(int(number), ""),
                historical=False,
                detected_at=detected_at,
            )
            if not clean_text(record.get("detected_at")):
                record["detected_at"] = detected_at
            record["historical"] = False

    def _discard_false_detail_items(
        self,
        project: Dict[str, Any],
        detailed: Set[int],
        ignored_detail_items: Set[int],
    ) -> Set[int]:
        """Remove registros criados apenas por uma vista de detalhe.

        A limpeza é deliberadamente conservadora: só remove itens sem sessão
        concluída, sem tempo gravado e que não aparecem em nenhuma vista válida
        na análise atual. Assim, o histórico real permanece intacto.
        """
        known = {
            int(value)
            for value in project.get("known_detailed", [])
            if str(value).isdigit() and int(value) > 0
        }
        candidates = (known & ignored_detail_items) - detailed
        active = project.get("active_session")
        active_numbers = (
            {int(value) for value in active.get("items", []) if str(value).isdigit()}
            if isinstance(active, dict)
            else set()
        )
        items = project.get("items", {}) or {}
        removable: Set[int] = set()
        for number in candidates:
            record = items.get(str(number))
            if not isinstance(record, dict):
                removable.add(number)
                continue
            if record.get("sessions"):
                continue
            if self._safe_seconds(record.get("tracked_seconds")) > 0:
                continue
            if clean_text(record.get("finished_at")):
                continue
            if bool(record.get("historical", False)):
                continue
            removable.add(number)

        if removable:
            for number in removable:
                items.pop(str(number), None)
            known -= removable
            project["known_detailed"] = numeric_sort(known)

        if active_numbers and active_numbers.issubset(removable):
            project["active_session"] = None

        return known

    def _statistics_items(
        self, project: Dict[str, Any], project_key: str, current_project_key: str
    ) -> List[Dict[str, Any]]:
        item_records = project.get("items", {})
        scan_known = {
            int(value)
            for value in project.get("last_scan_detailed", [])
            if str(value).isdigit() and int(value) > 0
        }
        known = set(scan_known) or {
            int(value)
            for value in project.get("known_detailed", [])
            if str(value).isdigit() and int(value) > 0
        }
        if not scan_known:
            for key in item_records:
                try:
                    known.add(int(key))
                except Exception:
                    continue

        active = project.get("active_session")
        active_numbers = (
            numeric_sort(active.get("items", [])) if isinstance(active, dict) else []
        )
        active_elapsed = (
            self._safe_seconds(active.get("elapsed_seconds"))
            if isinstance(active, dict)
            else 0.0
        )
        active_share = active_elapsed / len(active_numbers) if active_numbers else 0.0
        active_is_live = project_key == current_project_key and self._active_is_live(project)

        statistics: List[Dict[str, Any]] = []
        for number in numeric_sort(known):
            record = self._item_record(project, number, "", historical=True)
            is_active = number in active_numbers
            seconds = self._safe_seconds(record.get("tracked_seconds")) + (
                active_share if is_active else 0.0
            )
            statistics.append(
                {
                    "number": number,
                    "tag": clean_text(record.get("tag")),
                    "detected_at": clean_text(record.get("detected_at")),
                    "finished_at": clean_text(record.get("finished_at")),
                    "seconds": round(seconds, 1),
                    "active": bool(is_active and active_is_live),
                    "paused": bool(is_active and not active_is_live),
                    "historical": bool(record.get("historical", False)),
                    "pages": numeric_sort(record.get("pages", [])),
                }
            )

        def sort_key(entry: Dict[str, Any]):
            detected = clean_text(entry.get("detected_at"))
            priority = 2 if entry.get("active") else 1 if entry.get("paused") else 0
            return (priority, detected, int(entry.get("number", 0)))

        statistics.sort(key=sort_key, reverse=True)
        return statistics

    @staticmethod
    def _daily_summary(project: Dict[str, Any]) -> List[Dict[str, Any]]:
        visible = {
            int(value)
            for value in project.get("last_scan_detailed", [])
            if str(value).isdigit() and int(value) > 0
        }
        grouped: Dict[str, Set[int]] = {}
        for record in (project.get("items", {}) or {}).values():
            if not isinstance(record, dict):
                continue
            detected = parse_iso(record.get("detected_at")) or parse_iso(
                record.get("finished_at")
            )
            if detected is None:
                continue
            try:
                number = int(record.get("number"))
            except Exception:
                continue
            if visible and number not in visible:
                continue
            date_key = detected.date().isoformat()
            grouped.setdefault(date_key, set()).add(number)
        return [
            {
                "date": date_key,
                "count": len(numbers),
                "numbers": numeric_sort(numbers),
            }
            for date_key, numbers in sorted(grouped.items(), reverse=True)
        ]

    @staticmethod
    def _component_daily_summary(project: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Resume o avanço efetivo por componente agrupado por dia."""
        grouped: Dict[str, Dict[str, Any]] = {}
        events = project.get("component_metric_events", []) or []
        for event in events:
            if not isinstance(event, dict):
                continue
            when = parse_iso(event.get("detected_at"))
            if when is None:
                continue
            try:
                count = max(0, int(event.get("count") or 0))
            except Exception:
                count = 0
            if count <= 0:
                continue
            date_key = when.date().isoformat()
            bucket = grouped.setdefault(date_key, {"count": 0, "numbers": []})
            bucket["count"] += count
            for number in event.get("numbers", []) or []:
                text = clean_text(number)
                if text and text not in bucket["numbers"]:
                    bucket["numbers"].append(text)

        return [
            {
                "date": date_key,
                "count": int(grouped[date_key]["count"]),
                "numbers": sorted(
                    grouped[date_key]["numbers"],
                    key=lambda value: tuple(
                        int(part) if part.isdigit() else 0
                        for part in str(value).replace(",", ".").split(".")
                    ),
                ),
                "items": [],
            }
            for date_key in sorted(grouped, reverse=True)
        ]

    @staticmethod
    def _component_sets_summary(project: Dict[str, Any]) -> List[Dict[str, Any]]:
        sets = project.get("component_sets", []) or []
        output: List[Dict[str, Any]] = []
        for entry in sets:
            if not isinstance(entry, dict):
                continue
            try:
                number = int(entry.get("number"))
                detailed = max(0, int(entry.get("detailed") or 0))
                total = max(0, int(entry.get("total") or 0))
            except Exception:
                continue
            if number <= 0 or total <= 0:
                continue
            detailed = min(detailed, total)
            output.append(
                {
                    "number": number,
                    "tag": clean_text(entry.get("tag")),
                    "detailed": detailed,
                    "total": total,
                    "progress": round((detailed / total) * 100.0, 1) if total else 0.0,
                    "active": bool(detailed > 0 and detailed < total),
                    "completed": bool(detailed >= total),
                }
            )
        output.sort(key=lambda item: int(item.get("number", 0)))
        return output

    def _update_component_history(
        self,
        project: Dict[str, Any],
        result: Dict[str, Any],
        timestamp: str,
    ) -> None:
        """Registra avanço por componente sem depender do maior número visto.

        O total de cada conjunto é o maior índice sequencial (1.126 => 126).
        O avanço usa a cobertura das posições reais encontradas no modelo,
        projetada sobre esse total. Assim, uma nova posição abaixo de 1.98
        também aumenta o progresso e entra na estatística diária.
        """
        maxima_raw = result.get("component_max_by_parent", {}) or {}
        detected_raw = result.get("component_detailed_by_parent", {}) or {}
        effective_raw = result.get("component_effective_by_parent", {}) or {}
        sets_raw = result.get("component_sets", []) or []

        maxima: Dict[str, int] = {}
        detected: Dict[str, Set[int]] = {}
        effective: Dict[str, int] = {}
        for key, value in maxima_raw.items():
            try:
                parent = str(int(key))
                maximum = max(0, int(value or 0))
            except Exception:
                continue
            if maximum <= 0:
                continue
            maxima[parent] = maximum
            values = detected_raw.get(key, detected_raw.get(parent, []))
            children: Set[int] = set()
            if isinstance(values, (list, tuple, set)):
                for raw_child in values:
                    try:
                        child = int(raw_child)
                    except Exception:
                        continue
                    if 0 < child <= maximum:
                        children.add(child)
            detected[parent] = children
            try:
                count = int(effective_raw.get(key, effective_raw.get(parent, 0)) or 0)
            except Exception:
                count = 0
            effective[parent] = max(0, min(maximum, count))

        normalized_sets: List[Dict[str, Any]] = []
        for parent, maximum in sorted(maxima.items(), key=lambda item: int(item[0])):
            normalized_sets.append(
                {
                    "number": int(parent),
                    "tag": "",
                    "detailed": int(effective.get(parent, 0)),
                    "total": int(maximum),
                }
            )

        previous_detected_raw = project.get("component_detected_by_parent")
        previous_effective_raw = project.get("component_effective_by_parent")
        schema = int(project.get("component_history_schema") or 0)
        initialized = (
            schema >= 3
            and isinstance(previous_detected_raw, dict)
            and isinstance(previous_effective_raw, dict)
        )

        previous_detected: Dict[str, Set[int]] = {}
        previous_effective: Dict[str, int] = {}
        if initialized:
            for parent in maxima:
                values = previous_detected_raw.get(parent, []) or []
                children: Set[int] = set()
                if isinstance(values, (list, tuple, set)):
                    for raw_child in values:
                        try:
                            child = int(raw_child)
                        except Exception:
                            continue
                        if 0 < child <= maxima[parent]:
                            children.add(child)
                previous_detected[parent] = children
                try:
                    old_count = int(previous_effective_raw.get(parent, 0) or 0)
                except Exception:
                    old_count = 0
                previous_effective[parent] = max(0, min(maxima[parent], old_count))

        metric_events = project.get("component_metric_events")
        if not isinstance(metric_events, list):
            metric_events = []
        current_number_labels = {
            f"{parent}.{child}"
            for parent in sorted(detected, key=int)
            for child in sorted(detected[parent])
        }
        pruned_metric_events: List[Dict[str, Any]] = []
        for event in metric_events:
            if not isinstance(event, dict):
                continue
            event_numbers = [
                self._component_label(value)
                for value in event.get("numbers", []) or []
                if self._component_label(value)
            ]
            if event_numbers and not any(
                number in current_number_labels for number in event_numbers
            ):
                continue
            pruned_metric_events.append(event)
        metric_events = pruned_metric_events

        if not initialized:
            # Primeira leitura da métrica corrigida: registra o estado atual
            # como entrada inicial, sem texto explicativo na interface.
            total_now = sum(effective.values())
            if total_now > 0:
                numbers = [
                    f"{parent}.{child}"
                    for parent in sorted(detected, key=int)
                    for child in sorted(detected[parent])
                ]
                metric_events.append(
                    {
                        "detected_at": timestamp,
                        "count": int(total_now),
                        "numbers": numbers,
                        "initial": True,
                    }
                )
            project["component_history_started_at"] = timestamp
            project["component_history_schema"] = 3
            project["component_baseline_count"] = 0
            project["component_baseline_by_parent"] = {}
        else:
            new_numbers: List[str] = []
            for parent, current_children in detected.items():
                old_children = previous_detected.get(parent, set())
                for child in sorted(current_children - old_children):
                    new_numbers.append(f"{parent}.{child}")

            delta_total = 0
            for parent in maxima:
                delta_total += max(
                    0, int(effective.get(parent, 0)) - int(previous_effective.get(parent, 0))
                )
            if delta_total > 0:
                metric_events.append(
                    {
                        "detected_at": timestamp,
                        "count": int(delta_total),
                        "numbers": new_numbers,
                        "initial": False,
                    }
                )

        project["component_metric_events"] = metric_events[-10000:]
        project["component_max_by_parent"] = maxima
        project["component_detected_by_parent"] = {
            parent: numeric_sort(children) for parent, children in detected.items()
        }
        project["component_effective_by_parent"] = {
            parent: int(effective.get(parent, 0)) for parent in maxima
        }
        project["component_frontier_by_parent"] = {
            parent: max(children, default=0) for parent, children in detected.items()
        }
        project["component_sets"] = normalized_sets

    @staticmethod
    def _cota_ignore_key(entry: Dict[str, Any]) -> str:
        if not isinstance(entry, dict):
            return ""
        try:
            number = int(entry.get("number"))
        except Exception:
            number = 0
        try:
            child = int(entry.get("child"))
        except Exception:
            child = 0
        try:
            page = int(entry.get("page"))
        except Exception:
            page = 0
        view = clean_text(entry.get("view")).casefold()
        label = clean_text(entry.get("label")).casefold()
        if not number and not label:
            return ""
        return "|".join(str(value) for value in (number, child, page, view, label))

    def _cota_ignored_keys(self, project_key: str) -> Set[str]:
        project = self.data.get("projects", {}).get(clean_text(project_key))
        if not isinstance(project, dict):
            return set()
        return {
            clean_text(value)
            for value in project.get("cota_ignored", []) or []
            if clean_text(value)
        }

    def ignore_cota_item(
        self, project_key: str, item: Dict[str, Any]
    ) -> Dict[str, Any]:
        key = clean_text(project_key)
        project = self.data.get("projects", {}).get(key)
        if not isinstance(project, dict):
            return {"ok": False, "message": "Projeto atual nao encontrado."}
        ignore_key = self._cota_ignore_key(item)
        if not ignore_key:
            return {"ok": False, "message": "Item de cota invalido."}
        ignored = self._cota_ignored_keys(key)
        ignored.add(ignore_key)
        project["cota_ignored"] = sorted(ignored)
        self._save()
        return {"ok": True, "ignored_key": ignore_key}

    def apply_cota_ignores(self, result: Dict[str, Any]) -> None:
        project_key = clean_text(result.get("project_key"))
        ignored = self._cota_ignored_keys(project_key)
        if not ignored:
            return
        summary = result.get("cota_summary")
        if not isinstance(summary, dict):
            return

        def keep(entry: Any) -> bool:
            return not isinstance(entry, dict) or self._cota_ignore_key(entry) not in ignored

        filtered_missing = [
            item for item in summary.get("missing", []) or [] if keep(item)
        ]
        filtered_views: List[Dict[str, Any]] = []
        for view in summary.get("views", []) or []:
            if not isinstance(view, dict):
                continue
            view_copy = dict(view)
            view_missing = [
                item for item in view_copy.get("missing", []) or [] if keep(item)
            ]
            view_copy["missing"] = view_missing
            view_copy["missing_count"] = len(view_missing)
            try:
                required_count = int(view_copy.get("required_count", 0) or 0)
            except Exception:
                required_count = 0
            view_copy["dimensioned_count"] = max(0, required_count - len(view_missing))
            filtered_views.append(view_copy)

        summary["missing"] = filtered_missing
        summary["missing_count"] = len(filtered_missing)
        summary["views"] = filtered_views
        cut_missing = [
            item for item in summary.get("cut_missing", []) or []
            if isinstance(item, dict)
        ]
        pending_views = {
            (str(item.get("view") or ""), int(item.get("page") or 0))
            for item in [*filtered_missing, *cut_missing]
            if isinstance(item, dict)
        }
        summary["pending_views_count"] = len(pending_views)
        summary["ignored_manual_count"] = len(ignored)
        result["cota_summary"] = summary
        result["cota_items"] = filtered_missing

    def project_list(self, current_project_key: str = "") -> List[Dict[str, Any]]:
        projects = self.data.get("projects", {}) or {}
        output: List[Dict[str, Any]] = []
        for key, project in projects.items():
            if not isinstance(project, dict):
                continue
            known = {
                int(value)
                for value in project.get("known_detailed", [])
                if str(value).isdigit() and int(value) > 0
            }
            output.append(
                {
                    "project_key": clean_text(key),
                    "project_number": clean_text(project.get("project_number")),
                    "display_name": self._project_label(project),
                    "model_name": clean_text(project.get("model_name")),
                    "drawing_name": clean_text(project.get("drawing_name")),
                    "last_analysis_at": clean_text(project.get("last_analysis_at")),
                    "detailed_count": len(known),
                    "current": clean_text(key) == clean_text(current_project_key),
                }
            )
        output.sort(
            key=lambda entry: (
                1 if entry.get("current") else 0,
                clean_text(entry.get("last_analysis_at")),
            ),
            reverse=True,
        )
        return output

    def snapshot(
        self, project_key: str, current_project_key: str = ""
    ) -> Dict[str, Any]:
        key = clean_text(project_key)
        project = self.data.get("projects", {}).get(key)
        if not isinstance(project, dict):
            return {"ok": False, "message": "Projeto não encontrado no histórico."}
        component_time_sets, component_times = self._component_time_summaries(
            project, key, clean_text(current_project_key)
        )
        return {
            "ok": True,
            "project_key": key,
            "project_number": clean_text(project.get("project_number")),
            "display_name": self._project_label(project),
            "statistics_items": self._statistics_items(
                project, key, clean_text(current_project_key)
            ),
            "statistics_daily": self._daily_summary(project),
            "statistics_mode": clean_text(project.get("progress_mode")) or "conjunto",
            "statistics_component_daily": self._component_daily_summary(project),
            "statistics_component_sets": self._component_sets_summary(project),
            "statistics_component_time_sets": component_time_sets,
            "statistics_component_times": component_times,
            "component_history_started_at": clean_text(project.get("component_history_started_at")),
            "component_baseline_count": int(project.get("component_baseline_count") or 0),
            "component_history_schema": int(project.get("component_history_schema") or 0),
            "history_projects": self.project_list(current_project_key),
            "last_analysis_at": clean_text(project.get("last_analysis_at")),
        }

    def update(
        self, result: Dict[str, Any], interval_seconds: int
    ) -> Dict[str, Any]:
        now_dt = datetime.now().astimezone()
        timestamp = now_dt.isoformat(timespec="milliseconds")
        project_key = clean_text(result.get("project_key")) or "default"
        projects = self.data.setdefault("projects", {})
        project = projects.get(project_key)
        first_observation = not isinstance(project, dict)
        if first_observation:
            project = {
                "project_key": project_key,
                "project_number": clean_text(result.get("project_number")),
                "model_path": clean_text(result.get("model_path")),
                "model_name": clean_text(result.get("model_name")),
                "drawing_name": clean_text(result.get("drawing_name")),
                "known_detailed": [],
                "last_scan_detailed": [],
                "items": {},
                "active_session": None,
                "component_items": {},
                "component_active_session": None,
                "component_time_schema": 1,
                "created_at": timestamp,
            }
            projects[project_key] = project

        project["project_number"] = (
            clean_text(result.get("project_number"))
            or clean_text(project.get("project_number"))
        )
        project["model_path"] = clean_text(result.get("model_path"))
        project["model_name"] = clean_text(result.get("model_name"))
        project["drawing_name"] = clean_text(result.get("drawing_name"))
        # Progresso por componente soma todas as folhas do projeto: trocar de
        # folha não zera o que está nas outras, mas reanalisar a MESMA folha
        # substitui a contribuição dela (a lista de exceção continua reduzindo).
        self._apply_multisheet_component_progress(project, result)
        scan_total = max(0, int(result.get("total") or 0))
        scan_detailed = max(0, int(result.get("detailed") or 0))
        scan_missing = max(0, int(result.get("missing") or 0))
        scan_progress = 0.0
        if scan_total > 0:
            scan_progress = max(0.0, min(100.0, (scan_detailed / scan_total) * 100.0))
        project["last_scan_total"] = scan_total
        project["last_scan_detailed_count"] = scan_detailed
        project["last_scan_missing_count"] = scan_missing
        project["last_scan_progress"] = round(scan_progress, 1)
        self._stamp_active_runtime(project)

        tags = self._tag_map(result)
        pages_by_item: Dict[int, List[int]] = {}
        for entry in result.get("tracking_detailed_items", result.get("detailed_items", [])) or []:
            try:
                number = int(entry.get("number"))
                pages_by_item[number] = numeric_sort(entry.get("pages", []))
            except Exception:
                continue
        scan_sheet_total = self._sheet_total_from_result(result)
        project["last_scan_sheet_total"] = scan_sheet_total
        project["sheet_total"] = scan_sheet_total
        detailed = {
            int(value)
            for value in result.get("tracking_detailed_numbers", result.get("detailed_numbers", []))
            if int(value) > 0
        }
        known = {
            int(value)
            for value in project.get("known_detailed", [])
            if int(value) > 0
        }
        previous_scan = {
            int(value)
            for value in project.get("last_scan_detailed", [])
            if str(value).isdigit() and int(value) > 0
        }
        self._reconcile_active_with_current_detail(project, detailed)
        interval = max(10, min(3600, int(interval_seconds or 15)))
        self._accrue_active(project, now_dt, interval)

        ignored_detail_items = {
            int(value)
            for value in result.get("ignored_detail_items", [])
            if str(value).isdigit() and int(value) > 0
        }
        known = self._discard_false_detail_items(
            project, detailed, ignored_detail_items
        )
        item_views_raw = result.get("item_views", {}) or {}
        item_views: Dict[int, Set[str]] = {}
        for number, views in item_views_raw.items():
            try:
                item_views[int(number)] = {
                    clean_text(value) for value in (views or []) if clean_text(value)
                }
            except Exception:
                continue

        if first_observation:
            # Não inventa data ou tempo para itens já existentes antes do monitoramento.
            for number in numeric_sort(detailed):
                record = self._item_record(
                    project, number, tags.get(number, ""), historical=True
                )
                if number in pages_by_item:
                    record["pages"] = pages_by_item[number]
            project["known_detailed"] = numeric_sort(detailed)
            project["last_scan_detailed"] = numeric_sort(detailed)
        else:
            for number in numeric_sort(detailed):
                record = self._item_record(
                    project,
                    number,
                    tags.get(number, ""),
                    historical=number not in known,
                )
                if number in pages_by_item:
                    record["pages"] = pages_by_item[number]

            scan_new_items = detailed - previous_scan if previous_scan else set()
            new_items = (detailed - known) | scan_new_items
            if new_items:
                new_views: Set[str] = set()
                for number in new_items:
                    new_views.update(item_views.get(number, set()))

                active = project.get("active_session")
                active_views = (
                    {
                        clean_text(value)
                        for value in active.get("view_keys", [])
                        if clean_text(value)
                    }
                    if isinstance(active, dict)
                    else set()
                )
                same_cut = bool(
                    self._active_is_live(project)
                    and active_views
                    and new_views
                    and active_views.intersection(new_views)
                )
                if isinstance(active, dict) and active.get("items") and same_cut:
                    self._merge_active(
                        project, new_items, new_views, timestamp, tags
                    )
                else:
                    if isinstance(active, dict) and active.get("items"):
                        finished_at = (
                            timestamp
                            if self._active_is_live(project)
                            else clean_text(project.get("last_tick_at")) or timestamp
                        )
                        self._finish_active(project, finished_at)
                    self._start_active(
                        project, new_items, new_views, timestamp, tags
                    )

            project["known_detailed"] = numeric_sort(known | detailed)
            project["last_scan_detailed"] = numeric_sort(detailed)

        project["runtime_id"] = self.runtime_id
        project["last_tick_at"] = timestamp
        project["progress_mode"] = clean_text(result.get("progress_mode")) or "conjunto"
        if project["progress_mode"] == "subitem":
            # A migração de tempo usa o último levantamento já existente da R41;
            # só depois gravamos a data da análise atual.
            self._update_component_timing(
                project, result, timestamp, now_dt, interval
            )
            self._update_component_history(project, result, timestamp)
        project["last_analysis_at"] = timestamp

        active = project.get("active_session")
        active_numbers = (
            numeric_sort(active.get("items", []))
            if self._active_is_live(project)
            else []
        )
        active_elapsed = (
            self._safe_seconds(active.get("elapsed_seconds"))
            if self._active_is_live(project)
            else 0.0
        )
        active_share = active_elapsed / len(active_numbers) if active_numbers else 0.0
        current_items = [
            {
                "number": number,
                "tag": tags.get(number, ""),
                "pages": pages_by_item.get(number, []),
            }
            for number in active_numbers
        ]

        self._save()
        snapshot = self.snapshot(project_key, project_key)
        return {
            "statistics_items": snapshot.get("statistics_items", []),
            "statistics_daily": snapshot.get("statistics_daily", []),
            "statistics_mode": snapshot.get("statistics_mode", "conjunto"),
            "statistics_component_daily": snapshot.get("statistics_component_daily", []),
            "statistics_component_sets": snapshot.get("statistics_component_sets", []),
            "statistics_component_time_sets": snapshot.get("statistics_component_time_sets", []),
            "statistics_component_times": snapshot.get("statistics_component_times", []),
            "component_history_started_at": snapshot.get("component_history_started_at", ""),
            "component_baseline_count": snapshot.get("component_baseline_count", 0),
            "component_history_schema": snapshot.get("component_history_schema", 0),
            "history_projects": snapshot.get("history_projects", []),
            "statistics_project_key": project_key,
            "current_items": current_items,
            "current_started_at": (
                clean_text(active.get("started_at"))
                if isinstance(active, dict)
                else ""
            ),
            "current_elapsed_seconds": round(active_elapsed, 1),
            "current_item_seconds": round(active_share, 1),
            "tracking_started": not first_observation,
        }

    def complete_active(
        self, project_key: str, interval_seconds: int
    ) -> Dict[str, Any]:
        key = clean_text(project_key)
        project = self.data.get("projects", {}).get(key)
        if not isinstance(project, dict):
            return {"ok": False, "message": "Projeto atual não encontrado no histórico."}

        active = project.get("active_session")
        if not isinstance(active, dict) or not active.get("items"):
            return {"ok": False, "message": "Não há item em detalhamento para concluir."}

        now_dt = datetime.now().astimezone()
        interval = max(10, min(3600, int(interval_seconds or 15)))
        self._accrue_active(project, now_dt, interval)
        self._accrue_component_active(project, now_dt, interval)
        timestamp = now_dt.isoformat(timespec="milliseconds")
        project["component_last_tick_at"] = timestamp
        completed_numbers = numeric_sort(active.get("items", []))
        self._finish_active(project, timestamp)
        project["runtime_id"] = self.runtime_id
        project["last_tick_at"] = timestamp
        project["last_analysis_at"] = timestamp
        self._save()

        snapshot = self.snapshot(key, key)
        return {
            "ok": True,
            "completed_numbers": completed_numbers,
            "statistics_items": snapshot.get("statistics_items", []),
            "statistics_daily": snapshot.get("statistics_daily", []),
            "statistics_mode": snapshot.get("statistics_mode", "conjunto"),
            "statistics_component_daily": snapshot.get("statistics_component_daily", []),
            "statistics_component_sets": snapshot.get("statistics_component_sets", []),
            "statistics_component_time_sets": snapshot.get("statistics_component_time_sets", []),
            "statistics_component_times": snapshot.get("statistics_component_times", []),
            "component_history_started_at": snapshot.get("component_history_started_at", ""),
            "component_baseline_count": snapshot.get("component_baseline_count", 0),
            "component_history_schema": snapshot.get("component_history_schema", 0),
            "history_projects": snapshot.get("history_projects", []),
            "statistics_project_key": key,
            "current_items": [],
            "current_started_at": "",
            "current_elapsed_seconds": 0.0,
            "current_item_seconds": 0.0,
        }

    def pause(self, project_key: str, interval_seconds: int) -> None:
        key = clean_text(project_key)
        project = self.data.get("projects", {}).get(key)
        if not isinstance(project, dict):
            return
        now_dt = datetime.now().astimezone()
        interval = max(10, int(interval_seconds or 15))
        self._accrue_active(project, now_dt, interval)
        self._accrue_component_active(project, now_dt, interval)
        timestamp = now_dt.isoformat(timespec="milliseconds")
        project["last_tick_at"] = timestamp
        project["component_last_tick_at"] = timestamp
        project["runtime_id"] = self.runtime_id
        self._save()

class Api:
    def __init__(self):
        self._config = load_config()
        self._logs: List[str] = []
        self._lock = Lock()
        self._bridge = TeklaBridge(self._log)
        self._history = DetailHistory(HISTORY_FILE)
        self._history_lock = Lock()
        self._last_project_key = ""

    def _log(self, message: Any) -> None:
        text = clean_text(message)
        if not text:
            return
        self._logs.append(f"[{now_time()}] {text}")
        self._logs = self._logs[-100:]

    def initial_state(self) -> Dict[str, Any]:
        return {
            "config": self._config,
            "logs": self._logs[-20:],
        }

    def connection_status(self) -> Dict[str, Any]:
        return self._bridge.connection_state(clean_text(self._config.get("tekla_root")))

    def save_settings(self, values: Dict[str, Any]) -> Dict[str, Any]:
        try:
            values = dict(values or {})
            old_config = dict(self._config)
            try:
                interval = int(values.get(
                    "intervalo_analise_segundos",
                    self._config.get("intervalo_analise_segundos", 15),
                ))
            except Exception:
                interval = 15
            values["intervalo_analise_segundos"] = max(10, min(3600, interval))

            self._config.update(values)
            save_config(self._config)
            for key in ("uda_principal", "uda_reserva", "tekla_root", "modo_progresso", "vistas_base_ignoradas"):
                if clean_text(old_config.get(key)) != clean_text(self._config.get(key)):
                    self._bridge.clear_expected_cache()
                    self._bridge.clear_temporary_pages_cache()
                    break
            self.apply_topmost(bool(self._config.get("manter_a_frente", True)))
            return {"ok": True, "config": self._config}
        except Exception as exc:
            return {"ok": False, "message": clean_text(exc)}

    @staticmethod
    def _page_number(value: Any) -> Optional[int]:
        try:
            number = int(value)
            return number if number > 0 else None
        except Exception:
            return None

    def _current_cota_pages(self, result: Dict[str, Any]) -> Set[int]:
        pages: Set[int] = set()
        for item in result.get("current_items", []) or []:
            if not isinstance(item, dict):
                continue
            for value in item.get("pages", []) or []:
                page = self._page_number(value)
                if page is not None:
                    pages.add(page)
        return pages

    @staticmethod
    def _expected_map_from_cota_context(context: Dict[str, Any]) -> Dict[int, str]:
        expected_map: Dict[int, str] = {}
        if not isinstance(context, dict):
            return expected_map
        for key in ("detailed_items", "missing_items", "current_items"):
            for entry in context.get(key, []) or []:
                if not isinstance(entry, dict):
                    continue
                try:
                    number = int(entry.get("number"))
                except Exception:
                    continue
                if number <= 0:
                    continue
                tag = clean_text(entry.get("tag"))
                if number not in expected_map or tag:
                    expected_map[number] = tag
        return expected_map

    def analyze(self) -> Dict[str, Any]:
        if not self._lock.acquire(blocking=False):
            return {"ok": False, "message": "Já existe uma análise em andamento."}
        try:
            self._logs = []
            self._log("Análise iniciada.")
            api_start = time.perf_counter()
            result = self._bridge.analyze(self._config)
            project_key = clean_text(result.get("project_key"))
            interval = int(self._config.get("intervalo_analise_segundos", 15) or 15)
            history_start = time.perf_counter()
            with self._history_lock:
                if self._last_project_key and self._last_project_key != project_key:
                    self._history.pause(self._last_project_key, interval)
                tracking = self._history.update(result, interval)
            history_seconds = time.perf_counter() - history_start
            result.update(tracking)
            performance = result.setdefault("performance", {})
            if isinstance(performance, dict):
                performance["history_seconds"] = round(history_seconds, 3)
                performance["api_total_seconds"] = round(
                    time.perf_counter() - api_start, 3
                )
            self._last_project_key = project_key
            if isinstance(performance, dict):
                self._log(
                    "Tempos: conexao {connect_seconds}s | modelo {model_seconds}s | "
                    "desenho {drawing_seconds}s | historico {history_seconds}s | total {api_total_seconds}s | fonte {source}".format(
                        connect_seconds=performance.get("connect_seconds", "?"),
                        model_seconds=performance.get("model_seconds", "?"),
                        drawing_seconds=performance.get("drawing_seconds", "?"),
                        history_seconds=performance.get("history_seconds", "?"),
                        api_total_seconds=performance.get("api_total_seconds", "?"),
                        source=(result.get("model_stats") or {}).get("source", "?"),
                    )
                )
            self._log(
                f"Concluído: {result['detailed']} de {result['total']} itens detalhados."
            )
            result["logs"] = self._logs[-30:]
            return result
        except Exception as exc:
            self._log(f"Falha: {exc}")
            self._log(traceback.format_exc())
            return {
                "ok": False,
                "message": clean_text(exc) or "Falha desconhecida.",
                "logs": self._logs[-30:],
            }
        finally:
            self._lock.release()

    def analyze_cota(self, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if not self._lock.acquire(blocking=False):
            return {"ok": False, "message": "Outra leitura do Tekla esta em andamento."}
        try:
            payload = dict(context or {})
            expected_map = self._expected_map_from_cota_context(payload)
            focus_pages = self._current_cota_pages(payload)
            requested_project_key = clean_text(payload.get("project_key"))
            self._log("Analise de cota iniciada.")
            result = self._bridge.analyze_cota(
                self._config, expected_map, focus_pages
            )
            if (
                requested_project_key
                and clean_text(result.get("project_key")) != requested_project_key
            ):
                return {
                    "ok": False,
                    "message": "O projeto mudou antes do fim da analise de cota.",
                }
            with self._history_lock:
                self._history.apply_cota_ignores(result)
            summary = result.get("cota_summary") or {}
            if isinstance(summary, dict):
                self._log(
                    "Cota: esperado {expected_source} | pagina {page_source} | "
                    "paginas {pages_seconds}s | vistas {views_seconds}s | total {total_seconds}s".format(
                        expected_source=summary.get("expected_source", "?"),
                        page_source=summary.get("page_detection_source", "?"),
                        pages_seconds=summary.get("pages_seconds", "?"),
                        views_seconds=summary.get("views_seconds", "?"),
                        total_seconds=summary.get("total_seconds", "?"),
                    )
                )
            result["logs"] = self._logs[-30:]
            return result
        except Exception as exc:
            self._log(f"Falha na cota: {exc}")
            self._log(traceback.format_exc())
            return {
                "ok": False,
                "message": clean_text(exc) or "Falha desconhecida.",
                "logs": self._logs[-30:],
            }
        finally:
            self._lock.release()

    def ignore_cota_item(self, item: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        try:
            payload = dict(item or {})
            project_key = clean_text(payload.get("project_key")) or clean_text(
                self._last_project_key
            )
            if not project_key:
                return {"ok": False, "message": "Execute a analise antes de ocultar a cota."}
            with self._history_lock:
                return self._history.ignore_cota_item(project_key, payload)
        except Exception as exc:
            return {"ok": False, "message": clean_text(exc)}

    def complete_current(self) -> Dict[str, Any]:
        if not self._lock.acquire(blocking=False):
            return {"ok": False, "message": "Aguarde o término da análise em andamento."}
        try:
            project_key = clean_text(self._last_project_key)
            if not project_key:
                return {"ok": False, "message": "Execute a análise antes de concluir o item."}
            interval = int(self._config.get("intervalo_analise_segundos", 15) or 15)
            with self._history_lock:
                return self._history.complete_active(project_key, interval)
        except Exception as exc:
            return {"ok": False, "message": clean_text(exc)}
        finally:
            self._lock.release()

    def get_project_history(self, project_key: str) -> Dict[str, Any]:
        try:
            with self._history_lock:
                return self._history.snapshot(
                    clean_text(project_key), self._last_project_key
                )
        except Exception as exc:
            return {"ok": False, "message": clean_text(exc)}

    def apply_topmost(self, enabled: bool) -> Dict[str, Any]:
        try:
            set_native_topmost(bool(enabled))
            return {"ok": True}
        except Exception:
            return {"ok": False}

    def minimize(self) -> None:
        try:
            if webview is not None and webview.windows:
                webview.windows[0].minimize()
        except Exception:
            pass

    def resize_window(self, width: Any, height: Any) -> Dict[str, Any]:
        try:
            new_width = max(APP_WIDTH, int(float(width or APP_WIDTH)))
            new_height = max(APP_HEIGHT, int(float(height or APP_HEIGHT)))
            if webview is not None and webview.windows:
                webview.windows[0].resize(new_width, new_height)
            return {"ok": True, "width": new_width, "height": new_height}
        except Exception as exc:
            return {"ok": False, "message": clean_text(exc)}

    def close(self) -> None:
        try:
            if self._last_project_key:
                with self._history_lock:
                    self._history.pause(
                        self._last_project_key,
                        int(self._config.get("intervalo_analise_segundos", 15) or 15),
                    )
        except Exception:
            pass
        try:
            if webview is not None and webview.windows:
                webview.windows[0].destroy()
        except Exception:
            pass


def main() -> None:
    if webview is None:
        print('pywebview nao instalado. Execute "Instalar dependencias.bat".')
        input("Pressione Enter para sair...")
        return
    if not HTML_FILE.exists():
        print(f"Interface não encontrada: {HTML_FILE}")
        input("Pressione Enter para sair...")
        return

    api = Api()
    window = webview.create_window(
        APP_NAME,
        html=HTML_FILE.read_text(encoding="utf-8"),
        js_api=api,
        width=APP_WIDTH,
        height=APP_HEIGHT,
        min_size=(APP_WIDTH, APP_HEIGHT),
        resizable=True,
        frameless=True,
        easy_drag=False,
        on_top=bool(api._config.get("manter_a_frente", True)),
    )
    webview.start(debug=False)


if __name__ == "__main__":
    main()
