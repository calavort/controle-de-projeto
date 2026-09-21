from __future__ import annotations

"""Cria o atalho do Controle de Projeto no Menu Iniciar.

O atalho aponta DIRETO para o pythonw.exe e passa o Controle_de_Projeto.pyw
como argumento. Nao ha .bat nem cmd no caminho de abertura - e por isso que nao
existe o flash preto.

Este proprio arquivo e .pyw: roda-lo tambem nao pisca console nenhum.
"""

import ctypes
import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
LANCADOR = RAIZ / "Controle_de_Projeto.pyw"
ICONE = RAIZ / "assets" / "Controle de Projeto Logo.ico"
ATALHO_ANTIGO = "Controle de Projetos Tekla.lnk"


def aviso(texto: str, flags: int = 0x40) -> None:
    try:
        ctypes.windll.user32.MessageBoxW(None, texto, "Controle de Projeto", flags)
    except Exception:
        pass


def pythonw() -> Path:
    executavel = Path(sys.executable)
    if executavel.name.lower() == "pythonw.exe":
        return executavel
    ao_lado = executavel.with_name("pythonw.exe")
    if not ao_lado.exists():
        raise FileNotFoundError(f"pythonw.exe nao encontrado em: {ao_lado}")
    return ao_lado


try:
    import win32com.client  # type: ignore

    if not LANCADOR.is_file():
        raise FileNotFoundError(f"Nao encontrei o lancador: {LANCADOR}")

    programas = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    programas.mkdir(parents=True, exist_ok=True)

    # O atalho antigo apontava para o .bat e piscava o console; sai de cena.
    antigo = programas / ATALHO_ANTIGO
    if antigo.exists():
        try:
            antigo.unlink()
        except OSError:
            pass

    shell = win32com.client.Dispatch("WScript.Shell")
    atalho = shell.CreateShortCut(str(programas / "Controle de Projeto.lnk"))
    atalho.Targetpath = str(pythonw())
    atalho.Arguments = f'"{LANCADOR}"'
    atalho.WorkingDirectory = str(RAIZ)
    if ICONE.exists():
        atalho.IconLocation = f"{ICONE},0"
    atalho.Description = "Controle de Projeto"
    atalho.save()

    aviso(
        "Atalho criado no Menu Iniciar.\n\n"
        "Ele aponta DIRETAMENTE para o pythonw.exe e chama o "
        "Controle_de_Projeto.pyw. Nao existe .bat no caminho de abertura, "
        "entao nao ha flash de console."
    )
except Exception as excecao:  # noqa: BLE001 - a mensagem e o retorno deste script
    aviso(
        "Nao foi possivel criar o atalho.\n\n"
        "Rode o 'Instalar dependencias.bat' uma vez e tente de novo.\n\n"
        f"Detalhe: {excecao}",
        0x10,
    )
