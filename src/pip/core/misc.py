from __future__ import annotations


def enum(*sequential: str, **named: str) -> type[object]:
    values: dict[str, object] = dict(zip(sequential, range(len(sequential))), **named)
    values["reverse_mapping"] = {value: key for key, value in values.items()}
    return type("Enum", (), values)
