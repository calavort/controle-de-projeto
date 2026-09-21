from __future__ import annotations

"""Abertura do Controle de Projeto, sem console e sem flash.

Arquivo .pyw: o Windows abre a extensao com o pythonw.exe, que nao tem janela
de console nenhuma - nem para piscar. Nao ha .bat nem cmd no caminho, que era
de onde vinha o flash preto.

Alem de abrir, este arquivo e a rede de seguranca da abertura: sem console, uma
falta de biblioteca ou um erro na subida nao teriam onde aparecer e o programa
apenas "nao abriria". Aqui os dois casos viram uma janela de aviso e um log.
"""

import ctypes
import os
import runpy
import sys
import traceback
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
PROGRAMA = RAIZ / "CONTROLE DE PROJETO.py"

if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))


def aviso(texto: str, titulo: str = "Controle de Projeto", erro: bool = True) -> None:
    if os.name != "nt":
        return
    try:
        ctypes.windll.user32.MessageBoxW(None, texto, titulo, 0x10 if erro else 0x40)
    except Exception:
        pass


def guardar_log_do_erro(excecao: BaseException) -> str:
    """Escreve o traceback onde de para achar depois.

    Vai para logs/ do proprio programa; nao dando (pasta somente leitura, por
    exemplo), cai no LOCALAPPDATA, que sempre aceita escrita.
    """
    for pasta in (RAIZ / "logs", Path(os.environ.get("LOCALAPPDATA", str(RAIZ))) / "Controle de Projeto"):
        try:
            pasta.mkdir(parents=True, exist_ok=True)
            caminho = pasta / "controle-projeto-erro.log"
            with caminho.open("a", encoding="utf-8-sig") as arquivo:
                arquivo.write("\n" + "=" * 72 + "\n")
                arquivo.write(f"Data: {datetime.now():%d/%m/%Y %H:%M:%S}\n")
                arquivo.write("".join(traceback.format_exception(
                    type(excecao), excecao, excecao.__traceback__)))
            return str(caminho)
        except Exception:
            continue
    return "(nao foi possivel gravar o log)"


def conferir_bibliotecas() -> None:
    faltando = []
    for modulo, pacote in (("flask", "Flask"), ("waitress", "waitress")):
        try:
            __import__(modulo)
        except Exception:
            faltando.append(pacote)
    if faltando:
        aviso(
            "As bibliotecas do Controle de Projeto ainda nao estao instaladas.\n\n"
            "Execute o arquivo 'Instalar dependencias.bat' uma vez e abra o programa de novo.\n\n"
            "Faltando: " + ", ".join(faltando)
        )
        raise SystemExit(1)


def main() -> None:
    if not PROGRAMA.is_file():
        aviso(f"Nao encontrei o programa nesta pasta:\n\n{PROGRAMA}")
        raise SystemExit(2)
    conferir_bibliotecas()
    # runpy porque o nome do arquivo tem espacos e nao pode ser importado.
    runpy.run_path(str(PROGRAMA), run_name="__main__")


try:
    main()
except SystemExit:
    raise
except BaseException as excecao:  # noqa: BLE001 - aqui e o ultimo lugar antes de sumir
    caminho = guardar_log_do_erro(excecao)
    aviso(
        "O Controle de Projeto encontrou um erro inesperado e foi fechado.\n\n"
        f"O log ficou em:\n{caminho}\n\n"
        f"Erro: {excecao}"
    )
