# -*- coding: utf-8 -*-
"""Leva o registro do trabalho para casa, por um repositório privado do GitHub.

O Notas de Engenharia sincroniza nota por nota, porque cada nota tem um `uid`
próprio e duas máquinas podem escrever a mesma nota. Aqui não é assim: o
registro é um banco SQLite de 23 tabelas cujos IDs são contados por máquina, e
o uso é de mão única — os projetos nascem no trabalho e em casa só se
acompanha. Então o que viaja é o registro INTEIRO, num pacote só.

Quem envia manda a foto de agora; quem recebe troca a sua pela de lá. Não há
mesclagem, e é de propósito: mesclar 23 tabelas com IDs que não batem entre as
máquinas criaria projetos em dobro, que é exatamente o que não pode acontecer.

A regra em uma frase: **o envio mais recente é o que vale**. Se um dia você
passar a lançar projetos nos dois lugares, esta peça deixa de servir — está
escrito assim na tela, não escondido aqui.

O pacote que sobe é o mesmo da Cópia de segurança. Descer é a mesma importação
— inclusive o backup automático que ela faz antes de trocar qualquer coisa.
"""

from __future__ import annotations

import base64
import json
import platform
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any

from ..paths import DATA_DIR
from .copia import ErroDeCopia, exportar_copia, importar_copia, resumo_do_registro

API = "https://api.github.com"
TEMPO_LIMITE = 30
PASTA_NO_REPO = "controle-de-projeto"
ARQUIVO_PACOTE = f"{PASTA_NO_REPO}/registro.zip"
ARQUIVO_ESTADO = f"{PASTA_NO_REPO}/estado.json"
LIMITE = 60 * 1024 * 1024

# Fora do banco de proposito: o banco INTEIRO viaja na sincronizacao, e o token
# nao pode ir junto para a outra maquina.
CONFIG_PATH = DATA_DIR / "sincronizacao.json"


class ErroDeSincronizacao(Exception):
    """Mensagem que vai para a tela, em português."""


# ------------------------------------------------------------------ configuração

def ler_config() -> dict[str, Any]:
    try:
        dados = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return dados if isinstance(dados, dict) else {}
    except (OSError, ValueError):
        return {}


def gravar_config(novo: dict[str, Any]) -> dict[str, Any]:
    atual = ler_config()
    atual.update(novo)
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    provisorio = CONFIG_PATH.with_suffix(".json.tmp")
    provisorio.write_text(json.dumps(atual, ensure_ascii=False, indent=2), encoding="utf-8")
    provisorio.replace(CONFIG_PATH)
    return atual


def estado_publico() -> dict[str, Any]:
    """O que a tela pode ver. O token nunca volta — só se ele existe."""
    config = ler_config()
    return {
        "ligada": bool(config.get("ligada")),
        "repositorio": str(config.get("repositorio") or ""),
        "tem_token": bool(config.get("token")),
        "maquina": str(config.get("maquina") or platform.node()),
        "ultimo_envio": str(config.get("ultimo_envio") or ""),
        "ultima_descida": str(config.get("ultima_descida") or ""),
        "registro": resumo_do_registro(),
    }


# ------------------------------------------------------------------- transporte

def _pedir(config: dict[str, Any], caminho: str, metodo: str = "GET",
           corpo: dict[str, Any] | None = None) -> Any:
    repositorio = str(config.get("repositorio") or "").strip().strip("/")
    token = str(config.get("token") or "").strip()
    if "/" not in repositorio:
        raise ErroDeSincronizacao("Informe o repositório no formato dono/nome.")
    if not token:
        raise ErroDeSincronizacao("Falta o token de acesso do GitHub.")

    url = f"{API}/repos/{repositorio}/{caminho.lstrip('/')}"
    if urllib.parse.urlparse(url).scheme != "https":
        raise ErroDeSincronizacao("A sincronização exige uma conexão HTTPS.")

    dados = json.dumps(corpo).encode("utf-8") if corpo is not None else None
    pedido = urllib.request.Request(url, data=dados, method=metodo, headers={
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "ControleDeProjeto-Sync",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(pedido, timeout=TEMPO_LIMITE) as resposta:
            bruto = resposta.read(LIMITE + 1)
    except urllib.error.HTTPError as erro:
        if erro.code == 404:
            return None                       # ainda não há nada lá; não é falha
        if erro.code in (401, 403):
            raise ErroDeSincronizacao(
                "O GitHub recusou o token. Confira se ele tem permissão "
                "Contents: Read and write neste repositório."
            ) from erro
        if erro.code == 409:
            raise ErroDeSincronizacao("O repositório está vazio. Crie-o com um README e tente de novo.") from erro
        raise ErroDeSincronizacao(f"O GitHub respondeu {erro.code}.") from erro
    except urllib.error.URLError as erro:
        raise ErroDeSincronizacao(f"Sem conexão com o GitHub: {erro.reason}") from erro

    if len(bruto) > LIMITE:
        raise ErroDeSincronizacao("A resposta do GitHub veio grande demais.")
    if not bruto:
        return None
    return json.loads(bruto.decode("utf-8"))


def _sha_de(config: dict[str, Any], caminho: str) -> str:
    """O SHA que o GitHub dá ao arquivo. Sem ele, gravar por cima é recusado."""
    atual = _pedir(config, f"contents/{urllib.parse.quote(caminho)}")
    if isinstance(atual, dict):
        return str(atual.get("sha") or "")
    return ""


def _gravar_arquivo(config: dict[str, Any], caminho: str, conteudo: bytes, mensagem: str) -> None:
    corpo: dict[str, Any] = {
        "message": mensagem,
        "content": base64.b64encode(conteudo).decode("ascii"),
    }
    sha = _sha_de(config, caminho)
    if sha:
        corpo["sha"] = sha
    _pedir(config, f"contents/{urllib.parse.quote(caminho)}", "PUT", corpo)


def _ler_arquivo(config: dict[str, Any], caminho: str) -> bytes | None:
    """Lê pela API de blobs: a de conteúdo corta em 1 MB e o registro passa disso."""
    referencia = _pedir(config, f"contents/{urllib.parse.quote(caminho)}")
    if not isinstance(referencia, dict):
        return None
    sha = str(referencia.get("sha") or "")
    if not sha:
        return None
    blob = _pedir(config, f"git/blobs/{sha}")
    if not isinstance(blob, dict) or blob.get("encoding") != "base64":
        raise ErroDeSincronizacao("O arquivo no GitHub veio num formato inesperado.")
    return base64.b64decode(blob.get("content") or "")


# ---------------------------------------------------------------------- ações

def olhar_la() -> dict[str, Any]:
    """O que existe no repositório agora, sem baixar o registro inteiro."""
    config = ler_config()
    estado = _pedir(config, f"contents/{urllib.parse.quote(ARQUIVO_ESTADO)}")
    if not isinstance(estado, dict):
        return {"existe": False}
    try:
        conteudo = json.loads(base64.b64decode(estado.get("content") or "").decode("utf-8"))
    except Exception:
        return {"existe": False}
    return {"existe": True, **conteudo}


def enviar() -> dict[str, Any]:
    """Manda o registro desta máquina para o repositório."""
    config = ler_config()
    if not config.get("ligada"):
        raise ErroDeSincronizacao("A sincronização está desligada.")
    try:
        pacote, _ = exportar_copia()
    except ErroDeCopia as erro:
        raise ErroDeSincronizacao(str(erro)) from erro

    agora = datetime.now().isoformat(timespec="seconds")
    maquina = str(config.get("maquina") or platform.node())
    estado = {
        "gerado_em": agora,
        "maquina": maquina,
        "registro": resumo_do_registro(),
        "tamanho": len(pacote),
    }
    _gravar_arquivo(config, ARQUIVO_PACOTE, pacote, f"Registro de {maquina} em {agora}")
    _gravar_arquivo(config, ARQUIVO_ESTADO,
                    json.dumps(estado, ensure_ascii=False, indent=2).encode("utf-8"),
                    f"Estado de {maquina} em {agora}")
    gravar_config({"ultimo_envio": agora})
    return estado


def receber(database: Any, forcar: bool = False) -> dict[str, Any]:
    """Traz o registro do repositório para esta máquina.

    O que estava aqui vai para data/backups/ antes da troca — quem faz isso é a
    própria importação da Cópia de segurança, que é reaproveitada inteira.
    """
    config = ler_config()
    if not config.get("ligada"):
        raise ErroDeSincronizacao("A sincronização está desligada.")

    la = olhar_la()
    if not la.get("existe"):
        raise ErroDeSincronizacao("Ainda não há registro no repositório.")

    gerado_em = str(la.get("gerado_em") or "")
    ja_recebido = str(config.get("ultima_descida_gerada_em") or "")
    if not forcar and gerado_em and gerado_em == ja_recebido:
        return {"novidade": False, "gerado_em": gerado_em, "registro": la.get("registro") or {}}

    pacote = _ler_arquivo(config, ARQUIVO_PACOTE)
    if not pacote:
        raise ErroDeSincronizacao("O pacote do registro não foi encontrado no repositório.")
    try:
        resultado = importar_copia(pacote, database)
    except ErroDeCopia as erro:
        raise ErroDeSincronizacao(str(erro)) from erro

    gravar_config({
        "ultima_descida": datetime.now().isoformat(timespec="seconds"),
        "ultima_descida_gerada_em": gerado_em,
    })
    return {"novidade": True, "gerado_em": gerado_em, "de": la.get("maquina", ""), **resultado}
