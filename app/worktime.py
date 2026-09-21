"""Cálculo de horas trabalhadas por projeto com base na jornada de trabalho.

Regras (definidas pelo usuário):
- Segunda a sexta: 07:30–17:18, com 1h de almoço (12:00–13:00) NÃO contada.
- Sábado: 07:30–16:30, com 1h de almoço (12:00–13:00) NÃO contada, e SÓ conta se o
  programa foi aberto naquele sábado (dia presente em `presence_days`).
- Domingo: não conta.
- Assume-se que o projetista trabalha continuamente no projeto ATIVO durante a jornada,
  desde que ele fica ativo até ser concluído ou até outro projeto ser criado/ativado
  (isso é modelado pelos "períodos ativos" do projeto).
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

WEEKDAY_START = time(7, 30)
WEEKDAY_END = time(17, 18)
SATURDAY_END = time(16, 30)
LUNCH_START = time(12, 0)
LUNCH_END = time(13, 0)


def _parse(value) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if not value:
        return None
    text = str(value)
    try:
        return datetime.fromisoformat(text).replace(tzinfo=None)
    except ValueError:
        # tenta sem timezone / formatos parciais
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(text[: len(fmt) + 2], fmt)
            except ValueError:
                continue
    return None


def _overlap_seconds(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> float:
    lo = max(a_start, b_start)
    hi = min(a_end, b_end)
    return max(0.0, (hi - lo).total_seconds())


def _day_window(day: date, presence_days: set[str]) -> tuple[datetime, datetime] | None:
    weekday = day.weekday()  # 0=segunda ... 6=domingo
    if weekday == 6:  # domingo
        return None
    if weekday == 5:  # sábado
        if day.isoformat() not in presence_days:
            return None
        return datetime.combine(day, WEEKDAY_START), datetime.combine(day, SATURDAY_END)
    return datetime.combine(day, WEEKDAY_START), datetime.combine(day, WEEKDAY_END)


def worked_seconds(start: datetime | None, end: datetime | None, presence_days: set[str]) -> float:
    if not start or not end or end <= start:
        return 0.0
    total = 0.0
    current = start.date()
    last = end.date()
    while current <= last:
        window = _day_window(current, presence_days)
        if window:
            win_start, win_end = window
            segment = _overlap_seconds(start, end, win_start, win_end)
            if segment > 0:
                lunch = _overlap_seconds(
                    max(start, win_start),
                    min(end, win_end),
                    datetime.combine(current, LUNCH_START),
                    datetime.combine(current, LUNCH_END),
                )
                segment -= lunch
            total += max(0.0, segment)
        current += timedelta(days=1)
    return total



def outside_schedule_seconds(start: datetime | None, end: datetime | None, presence_days: set[str]) -> int:
    """Retorna apenas o tempo corrido fora da jornada configurada.

    É usado para complementar o relógio quando o Controle de Projetos permanece
    aberto fora do expediente, sem duplicar o período que já é contabilizado
    pela jornada normal.
    """
    if not start or not end or end <= start:
        return 0
    raw = max(0.0, (end - start).total_seconds())
    scheduled = worked_seconds(start, end, set(presence_days or []))
    return int(round(max(0.0, raw - scheduled)))

def project_worked_seconds(periods, presence_days, now: datetime | None = None) -> int:
    """periods: iterável de (start_at, end_at); end_at None = período ainda aberto (usa `now`)."""
    reference = now or datetime.now()
    presence = set(presence_days or [])
    total = 0.0
    for start_raw, end_raw in periods:
        start = _parse(start_raw)
        end = _parse(end_raw) if end_raw else reference
        total += worked_seconds(start, end, presence)
    return int(round(total))
