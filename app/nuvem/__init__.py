"""O que liga o Controle de Projeto ao mundo de fora.

Dois assuntos, dois arquivos, o mesmo desenho do Notas de Engenharia:

- "copia"        guarda e devolve o registro inteiro num arquivo .zip
- "atualizador"  troca o programa pela versao publicada no GitHub

Nenhum deles toca no resto do programa: ambos entram por app/routes.py e
conversam com o banco pelo mesmo caminho que as demais telas usam.
"""

from .atualizador import (
    UpdateError,
    check_release,
    download_release,
    prepare_installer,
    read_version,
    start_installer,
)
from .copia import ErroDeCopia, exportar_copia, importar_copia, resumo_do_registro

__all__ = [
    "ErroDeCopia",
    "UpdateError",
    "check_release",
    "download_release",
    "exportar_copia",
    "importar_copia",
    "prepare_installer",
    "read_version",
    "resumo_do_registro",
    "start_installer",
]
