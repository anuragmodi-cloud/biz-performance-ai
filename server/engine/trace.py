"""StepTrace: records every step a compute function takes, in order.

This is the audit trail the admin dashboard shows for each ask, and what
hallucination_check.py cross-references narrated numbers against. A compute
function is expected to append a step for every load/filter/aggregate/formula
it performs -- the trace should let a human reconstruct the calculation by
hand from the CSVs, without having to trust the number in isolation.

serialize()'s output is deliberately two layers: a `tables_queried` +
`computations` summary up top (what got touched, what got calculated -- the
quick-scan view), and the full `steps` list underneath (the exact
load/filter/aggregate/formula sequence -- the drill-down view for verifying
the arithmetic by hand). Neither replaces the other.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Step:
    kind: str  # "load" | "filter" | "aggregate" | "formula" | "join" | "note"
    detail: str
    name: str | None = None  # short label for aggregate/formula steps, e.g. "cash_inflow" -- feeds `computations`
    rows_before: int | None = None
    rows_after: int | None = None
    result: Any = None

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "name": self.name,
            "detail": self.detail,
            "rows_before": self.rows_before,
            "rows_after": self.rows_after,
            "result": _jsonable(self.result),
        }


def _jsonable(value):
    """Best-effort conversion so Step.result survives json.dumps -- pandas/
    numpy scalars are common here and aren't natively JSON-serializable."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return round(value, 4)
    try:
        import numpy as np
        if isinstance(value, np.generic):
            return _jsonable(value.item())
    except ImportError:
        pass
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return str(value)


@dataclass
class StepTrace:
    steps: list[Step] = field(default_factory=list)
    tables_queried: list[str] = field(default_factory=list)

    def load(self, table: str, rows: int) -> None:
        if table not in self.tables_queried:
            self.tables_queried.append(table)
        self.steps.append(Step(kind="load", detail=table, rows_after=rows))

    def filter(self, detail: str, rows_before: int, rows_after: int) -> None:
        self.steps.append(Step(kind="filter", detail=detail, rows_before=rows_before, rows_after=rows_after))

    def aggregate(self, name: str, detail: str, result: Any) -> None:
        self.steps.append(Step(kind="aggregate", name=name, detail=detail, result=result))

    def formula(self, name: str, detail: str, result: Any) -> None:
        self.steps.append(Step(kind="formula", name=name, detail=detail, result=result))

    def join(self, detail: str) -> None:
        self.steps.append(Step(kind="join", detail=detail))

    def note(self, detail: str) -> None:
        self.steps.append(Step(kind="note", detail=detail))

    def computations(self) -> list[str]:
        """Named aggregate/formula steps, in order -- "what got calculated,"
        the summary line for the admin view."""
        return [s.name for s in self.steps if s.name]

    def to_list(self) -> list[dict]:
        return [s.to_dict() for s in self.steps]

    def to_dict(self) -> dict:
        return {
            "tables_queried": list(self.tables_queried),
            "computations": self.computations(),
            "steps": self.to_list(),
        }

    def all_numeric_results(self) -> list[float]:
        """Every numeric value that appeared anywhere in the trace -- what
        hallucination_check.py treats as "grounded" numbers a narration is
        allowed to state."""
        out: list[float] = []

        def collect(v):
            if isinstance(v, bool):
                return
            if isinstance(v, (int, float)):
                out.append(float(v))
            elif isinstance(v, dict):
                for x in v.values():
                    collect(x)
            elif isinstance(v, (list, tuple)):
                for x in v:
                    collect(x)

        for step in self.steps:
            collect(step.result)
        return out
