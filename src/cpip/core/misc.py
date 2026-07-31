from __future__ import annotations

from typing import Any


def enum(*sequential: str, **named: str) -> Any:
    values: dict[str, object] = dict(zip(sequential, range(len(sequential))), **named)
    values["reverse_mapping"] = {value: key for key, value in values.items()}
    return type("Enum", (), values)
