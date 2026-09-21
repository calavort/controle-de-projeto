from __future__ import annotations

from typing import Any

from .utils import clamp


DEFAULT_STAGES: list[dict[str, Any]] = [
    {"order": 1, "name": "Limpeza do modelo 3D para detalhamento", "weight": 5.0},
    {"order": 2, "name": "Identificação e numeração dos conjuntos no modelo 3D", "weight": 6.0},
    {"order": 3, "name": "Configuração das folhas do multidesenho", "weight": 6.0},
    {"order": 4, "name": "Preparação das vistas-base para lista de material e detalhamento", "weight": 6.0},
    {"order": 5, "name": "Iniciar detalhamento", "weight": 47.0},
    {"order": 6, "name": "Criação dos detalhes de peças para fabricação", "weight": 8.0},
    {"order": 7, "name": "Distribuição das vistas de detalhamento nas folhas", "weight": 10.0},
    {"order": 8, "name": "Distribuição dos detalhes de peças nas folhas", "weight": 4.0},
    {"order": 9, "name": "Numeração das soldas", "weight": 4.0},
    {"order": 10, "name": "Preenchimento da tabela de revisão e finalização do desenho", "weight": 4.0},
]

# Programas suportados e níveis de etapas (ver memória do projeto: roadmap-etapas-por-programa).
PROGRAMS = ("Tekla Structures", "SolidWorks", "ZWCAD")
STAGE_MODES = ("Simples", "Detalhada")
DEFAULT_PROGRAM = "Tekla Structures"
DEFAULT_STAGE_MODE = "Detalhada"

# Fluxo "Simples": genérico para qualquer programa (o projetista pode editar por programa).
SIMPLE_STAGES: list[dict[str, Any]] = [
    {"order": 1, "name": "Cadastrar o projeto", "weight": 10.0},
    {"order": 2, "name": "Ajustar o modelo", "weight": 20.0},
    {"order": 3, "name": "Iniciar detalhamento", "weight": 50.0},
    {"order": 4, "name": "Finalizar", "weight": 20.0},
]

# Fluxo "Detalhada" genérico (SolidWorks/ZWCAD): esqueleto neutro para o projetista editar.
GENERIC_DETAILED_STAGES: list[dict[str, Any]] = [
    {"order": 1, "name": "Cadastro e preparação do projeto", "weight": 8.0},
    {"order": 2, "name": "Preparação/ajuste do modelo", "weight": 12.0},
    {"order": 3, "name": "Configuração das folhas/pranchas", "weight": 10.0},
    {"order": 4, "name": "Iniciar detalhamento", "weight": 40.0},
    {"order": 5, "name": "Criação dos detalhes", "weight": 12.0},
    {"order": 6, "name": "Distribuição nas folhas", "weight": 10.0},
    {"order": 7, "name": "Revisão e finalização", "weight": 8.0},
]


def default_template_stages(program: str, mode: str) -> list[dict[str, Any]]:
    """Etapas padrão (semente) para uma combinação de programa × nível."""
    mode_norm = str(mode or "").strip().lower()
    program_norm = str(program or "").strip().lower()
    if mode_norm.startswith("simpl"):
        return [dict(item) for item in SIMPLE_STAGES]
    if program_norm.startswith("tekla"):
        return [dict(item) for item in DEFAULT_STAGES]
    return [dict(item) for item in GENERIC_DETAILED_STAGES]


STAGE_STATUS = ("Não iniciada", "Em andamento", "Concluída", "Pausada")


def default_weights() -> dict[str, float]:
    return {str(item["order"]): float(item["weight"]) for item in DEFAULT_STAGES}


def normalize_weights(weights: dict[str, float]) -> dict[str, float]:
    normalized = {str(key): max(0.0, float(value)) for key, value in weights.items()}
    total = sum(normalized.values())
    if total <= 0:
        return default_weights()
    return {key: round(value * 100.0 / total, 4) for key, value in normalized.items()}


def calculate_project_progress(stages: list[dict[str, Any]]) -> float:
    if not stages:
        return 0.0
    weight_total = sum(float(stage.get("weight") or 0) for stage in stages)
    if weight_total <= 0:
        return 0.0
    contribution = 0.0
    for stage in stages:
        percent = clamp(float(stage.get("internal_percent") or 0))
        weight = float(stage.get("weight") or 0)
        contribution += weight * (percent / 100.0)
    return round(clamp(contribution * 100.0 / weight_total), 2)


def current_stage(stages: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not stages:
        return None
    active_stages = [
        stage
        for stage in stages
        if stage.get("status") == "Em andamento" or 0 < float(stage.get("internal_percent") or 0) < 100
    ]
    if active_stages:
        return max(active_stages, key=lambda stage: int(stage.get("stage_order") or 0))
    for stage in stages:
        if float(stage.get("internal_percent") or 0) < 100:
            return stage
    return stages[-1]


def smooth_weights(current: dict[str, float], observed: dict[str, float], factor: float = 0.30) -> dict[str, float]:
    factor = max(0.0, min(1.0, factor))
    keys = sorted(set(current) | set(observed), key=lambda item: int(item) if str(item).isdigit() else 999)
    result = {}
    for key in keys:
        result[key] = (float(current.get(key, 0)) * (1.0 - factor)) + (float(observed.get(key, 0)) * factor)
    return normalize_weights(result)


def observed_weights_from_seconds(seconds_by_stage: dict[str, float]) -> dict[str, float]:
    total = sum(max(0.0, float(value)) for value in seconds_by_stage.values())
    if total <= 0:
        return default_weights()
    return normalize_weights({key: max(0.0, float(value)) * 100.0 / total for key, value in seconds_by_stage.items()})


def stage_update(
    order: int,
    percent: float,
    status: str,
    source: str,
    confidence: float,
    message: str,
) -> dict[str, Any]:
    return {
        "stage_order": int(order),
        "internal_percent": clamp(percent),
        "status": status if status in STAGE_STATUS else "Em andamento",
        "source": source,
        "confidence": max(0.0, min(1.0, float(confidence))),
        "message": message,
    }
