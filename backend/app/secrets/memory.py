from __future__ import annotations


class MemorySecretStore:
    """Process-local store for tests. It must never be selected in production."""

    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    def put(self, reference: str, value: str) -> None:
        self._values[reference] = value

    def get(self, reference: str) -> str:
        return self._values[reference]

    def delete(self, reference: str) -> None:
        self._values.pop(reference, None)

    def exists(self, reference: str) -> bool:
        return reference in self._values
