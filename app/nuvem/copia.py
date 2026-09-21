# -*- coding: utf-8 -*-
"""Cópia de segurança do registro inteiro, num arquivo só.

O registro do Controle de Projeto não é um punhado de arquivos soltos: é um
banco SQLite com 23 tabelas mais o JSON do Progresso de detalhamento. Guardar
"as notas" como o Notas de Engenharia faz não serve aqui — o que se guarda é o
banco, inteiro, do jeito que está.

A cópia sai num .zip com os dois arquivos e um manifesto que diz quando foi
tirada e o que tem dentro. Na volta, o manifesto é conferido antes de qualquer
coisa ser trocada: um .zip que não for uma cópia deste programa é recusado sem
tocar em nada.

Trocar o banco é a operação mais perigosa do programa. Por isso, antes de
escrever qualquer byte, o banco atual vai para data/backups/ pelo mesmo caminho
que o programa já usa — e é de lá que se volta se algo der errado no meio.
"""

from __future__ import annotations

import json
import sqlite3
import zipfile
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any

from ..paths import APP_DIR, BACKUP_DIR, DB_PATH, ensure_folders

MANIFESTO = "controle-de-projeto.json"
NOME_BANCO = "controle_projetos.sqlite"
NOME_ESTATISTICAS = "estatisticas_detalhamento.json"
ESTATISTICAS_PATH = APP_DIR / "progresso_detalhamento" / NOME_ESTATISTICAS

# Tabelas que valem como "tamanho" do registro. Servem para o usuario reconhecer
# a copia certa antes de restaurar - e para o programa recusar um banco vazio.
TABELAS_DE_RESUMO = ("projects", "project_stages", "activity_history", "work_sessions")


class ErroDeCopia(Exception):
    """Algo impediu guardar ou devolver a cópia. A mensagem vai para a tela."""


def _contar(conexao: sqlite3.Connection, tabela: str) -> int:
    try:
        return int(conexao.execute(f'SELECT COUNT(*) FROM "{tabela}"').fetchone()[0])
    except sqlite3.Error:
        return 0


def resumo_do_registro(caminho_banco: Path | None = None) -> dict[str, Any]:
    """Quantas linhas há em cada tabela que interessa. Usado no manifesto e na tela."""
    caminho = caminho_banco or DB_PATH
    if not caminho.is_file():
        return {tabela: 0 for tabela in TABELAS_DE_RESUMO}
    conexao = sqlite3.connect(caminho)
    try:
        return {tabela: _contar(conexao, tabela) for tabela in TABELAS_DE_RESUMO}
    finally:
        conexao.close()


def exportar_copia() -> tuple[bytes, str]:
    """Devolve o .zip da cópia e o nome sugerido do arquivo."""
    if not DB_PATH.is_file():
        raise ErroDeCopia("Ainda não há registro para guardar.")

    resumo = resumo_do_registro()
    agora = datetime.now()
    manifesto = {
        "programa": "Controle de Projeto",
        "formato": 1,
        "gerado_em": agora.isoformat(timespec="seconds"),
        "registro": resumo,
    }

    saco = BytesIO()
    with zipfile.ZipFile(saco, "w", zipfile.ZIP_DEFLATED) as pacote:
        pacote.writestr(MANIFESTO, json.dumps(manifesto, ensure_ascii=False, indent=2))
        # O banco vai por uma conexao de backup, e nao por copia de arquivo: assim
        # ele sai integro mesmo se alguem estiver gravando neste instante.
        pacote.writestr(NOME_BANCO, _banco_consistente())
        if ESTATISTICAS_PATH.is_file():
            pacote.writestr(NOME_ESTATISTICAS, ESTATISTICAS_PATH.read_bytes())

    nome = f"controle-de-projeto-{agora:%Y%m%d_%H%M%S}.zip"
    return saco.getvalue(), nome


def _banco_consistente() -> bytes:
    """O banco lido pela API de backup do SQLite, nunca no meio de uma gravação."""
    origem = sqlite3.connect(DB_PATH)
    destino = sqlite3.connect(":memory:")
    try:
        origem.backup(destino)
        temporario = BACKUP_DIR / "_copia_em_andamento.sqlite"
        temporario.unlink(missing_ok=True)
        arquivo = sqlite3.connect(temporario)
        try:
            destino.backup(arquivo)
        finally:
            arquivo.close()
        dados = temporario.read_bytes()
        temporario.unlink(missing_ok=True)
        return dados
    finally:
        origem.close()
        destino.close()


def _conferir_pacote(pacote: zipfile.ZipFile) -> dict[str, Any]:
    nomes = set(pacote.namelist())
    if MANIFESTO not in nomes or NOME_BANCO not in nomes:
        raise ErroDeCopia("Este arquivo não é uma cópia do Controle de Projeto.")
    try:
        manifesto = json.loads(pacote.read(MANIFESTO).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as erro:
        raise ErroDeCopia("O manifesto da cópia está ilegível.") from erro
    if manifesto.get("programa") != "Controle de Projeto":
        raise ErroDeCopia("Esta cópia é de outro programa.")
    return manifesto


def _copiar_banco(origem: Path, destino: Path) -> None:
    """Põe o conteúdo de um banco dentro do outro, pela API do SQLite.

    Trocar o arquivo no disco esbarra no Windows quando alguma conexão está
    aberta; por dentro do SQLite a substituição acontece com os bloqueios dele
    e vale também para quem já tinha o banco aberto.
    """
    entrada = sqlite3.connect(origem)
    saida = sqlite3.connect(destino, timeout=15)
    try:
        entrada.backup(saida)
    finally:
        entrada.close()
        saida.close()


def _ultimo_backup() -> Path | None:
    copias = sorted(
        BACKUP_DIR.glob("controle_projetos_*.sqlite"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    return copias[0] if copias else None


def _conferir_banco(dados: bytes) -> dict[str, int]:
    """Abre o banco da cópia num arquivo à parte e confere que ele presta."""
    ensure_folders()
    provisorio = BACKUP_DIR / "_copia_recebida.sqlite"
    provisorio.write_bytes(dados)
    try:
        conexao = sqlite3.connect(provisorio)
        try:
            estado = conexao.execute("PRAGMA integrity_check").fetchone()
            if not estado or str(estado[0]).lower() != "ok":
                raise ErroDeCopia("O banco dentro da cópia está danificado.")
            resumo = {tabela: _contar(conexao, tabela) for tabela in TABELAS_DE_RESUMO}
        finally:
            conexao.close()
        if resumo.get("projects", 0) <= 0:
            raise ErroDeCopia("A cópia não tem nenhum projeto. Nada foi alterado.")
        return resumo
    except sqlite3.Error as erro:
        raise ErroDeCopia("O arquivo de banco dentro da cópia não pôde ser lido.") from erro
    finally:
        provisorio.unlink(missing_ok=True)


def importar_copia(dados_zip: bytes, database: Any) -> dict[str, Any]:
    """Põe a cópia no lugar do registro atual.

    `database` é o ControleDatabase em uso: dele vêm o cadeado (para ninguém
    escrever no meio da troca), o backup de segurança e a recriação das tabelas
    — que é o que faz uma cópia mais antiga ganhar as colunas criadas depois.
    """
    try:
        pacote = zipfile.ZipFile(BytesIO(dados_zip))
    except zipfile.BadZipFile as erro:
        raise ErroDeCopia("O arquivo enviado não é um .zip válido.") from erro

    with pacote:
        manifesto = _conferir_pacote(pacote)
        banco = pacote.read(NOME_BANCO)
        resumo = _conferir_banco(banco)
        estatisticas = pacote.read(NOME_ESTATISTICAS) if NOME_ESTATISTICAS in pacote.namelist() else None

    ensure_folders()
    recebido = BACKUP_DIR / "_copia_aplicando.sqlite"
    with database._lock:
        # O registro de agora vai para data/backups/ ANTES de qualquer troca.
        database._backup_existing_database()
        guardado = _ultimo_backup()
        recebido.write_bytes(banco)
        try:
            # A troca vai pela API de backup do proprio SQLite, e nao renomeando
            # arquivos: no Windows, renomear um banco com qualquer conexao ainda
            # aberta falha com "arquivo em uso". Pelo SQLite, o conteudo e
            # substituido por dentro, respeitando os bloqueios.
            _copiar_banco(recebido, DB_PATH)
            if estatisticas is not None:
                ESTATISTICAS_PATH.parent.mkdir(parents=True, exist_ok=True)
                ESTATISTICAS_PATH.write_bytes(estatisticas)
            database.create_tables()
        except Exception as erro:
            # Deu errado no meio: o registro de antes volta do backup.
            if guardado and Path(guardado).is_file():
                try:
                    _copiar_banco(Path(guardado), DB_PATH)
                except Exception:
                    pass
            raise ErroDeCopia(f"Não foi possível aplicar a cópia: {erro}") from erro
        finally:
            recebido.unlink(missing_ok=True)

    return {
        "gerado_em": manifesto.get("gerado_em", ""),
        "registro": resumo,
        "estatisticas_restauradas": estatisticas is not None,
    }
