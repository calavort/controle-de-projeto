from __future__ import annotations

import os
import runpy
import socket
import sys
import threading
import webbrowser
import argparse
from pathlib import Path

from waitress import serve

from app.routes import create_app
from app.service import ControleService

PROGRESSO_DETALHAMENTO_ARG = "--progresso-detalhamento"


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _resource_root() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(str(sys._MEIPASS))  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent


def _progresso_detalhamento_script() -> Path:
    relative = Path("app") / "progresso_detalhamento" / "Progresso de detalhamento.py"
    candidates = [
        _resource_root() / relative,
        Path(sys.executable).resolve().parent / relative,
        Path.cwd() / relative,
        Path(__file__).resolve().parent / relative,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


def run_progresso_detalhamento() -> None:
    script_path = _progresso_detalhamento_script()
    if not script_path.is_file():
        print(f"Progresso de detalhamento incorporado nao foi encontrado: {script_path}")
        raise SystemExit(2)
    os.chdir(script_path.parent)
    runpy.run_path(str(script_path), run_name="__main__")


def _pyinstaller_dependency_hints() -> None:
    try:
        import webview  # noqa: F401
        import clr  # noqa: F401
    except Exception:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Controle de Projetos")
    parser.add_argument("--port", type=int, default=0, help="Porta local. Use 0 para escolher automaticamente.")
    parser.add_argument("--no-browser", action="store_true", help="Não abrir o navegador automaticamente.")
    parser.add_argument(PROGRESSO_DETALHAMENTO_ARG, action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.progresso_detalhamento:
        run_progresso_detalhamento()
        return

    service = ControleService()
    app = create_app(service)

    # Mantém a contagem do projeto ativo mesmo fora do expediente e mesmo quando
    # o usuário permanece em uma tela que não faz atualização automática.
    heartbeat_stop = threading.Event()
    service.heartbeat_runtime()

    def runtime_heartbeat() -> None:
        while not heartbeat_stop.wait(15.0):
            try:
                service.heartbeat_runtime()
            except Exception:
                # O relógio nunca deve impedir a abertura ou o uso do programa.
                pass

    threading.Thread(target=runtime_heartbeat, name="controle-runtime-clock", daemon=True).start()

    port = args.port or find_free_port()
    url = f"http://127.0.0.1:{port}"
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"Controle de Projetos iniciado em {url}")
    print("Pressione CTRL+C para encerrar.")
    # Servidor WSGI de produção (waitress): sem o aviso do servidor de desenvolvimento.
    try:
        serve(app, host="127.0.0.1", port=port, threads=8, _quiet=True)
    finally:
        heartbeat_stop.set()
        try:
            service.heartbeat_runtime()
        except Exception:
            pass


if __name__ == "__main__":
    main()
