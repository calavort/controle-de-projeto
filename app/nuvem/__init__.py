"""O que liga o Controle de Projeto ao mundo de fora.

Tres assuntos, tres arquivos, o mesmo desenho do Notas de Engenharia:

- "copia"          guarda e devolve o registro inteiro num arquivo .zip
- "atualizador"    troca o programa pela versao publicada no GitHub
- "sincronizacao"  leva o registro de uma maquina para a outra

Nenhum deles toca no resto do programa: todos entram por app/routes.py e
conversam com o banco pelo mesmo caminho que as demais telas usam.
"""

from .copia import ErroDeCopia, exportar_copia, importar_copia, resumo_do_registro

__all__ = ["ErroDeCopia", "exportar_copia", "importar_copia", "resumo_do_registro"]
