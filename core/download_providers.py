from dataclasses import dataclass
from threading import RLock
from typing import Callable, Optional


ProviderRunner = Callable[[object, list[str], dict], tuple[bool, bool, str]]
ProviderSelector = Callable[[object], bool]


@dataclass(frozen=True)
class DownloadProvider:
    name: str
    priority: int
    can_handle: ProviderSelector
    run_once: ProviderRunner


class DownloadProviderRegistry:
    def __init__(self):
        self._lock = RLock()
        self._providers: dict[str, DownloadProvider] = {}

    def register(
        self,
        name: str,
        *,
        can_handle: ProviderSelector,
        run_once: ProviderRunner,
        priority: int = 100,
    ) -> str:
        normalized = str(name or "").strip().lower()
        if not normalized:
            raise ValueError("provider name cannot be empty")
        entry = DownloadProvider(
            name=normalized,
            priority=int(priority),
            can_handle=can_handle,
            run_once=run_once,
        )
        with self._lock:
            self._providers[normalized] = entry
        return normalized

    def unregister(self, name: str) -> bool:
        normalized = str(name or "").strip().lower()
        if not normalized:
            return False
        with self._lock:
            return self._providers.pop(normalized, None) is not None

    def list_names(self) -> list[str]:
        with self._lock:
            return sorted(self._providers.keys())

    def resolve(self, worker: object) -> Optional[DownloadProvider]:
        with self._lock:
            providers = sorted(self._providers.values(), key=lambda item: (item.priority, item.name))
        for entry in providers:
            try:
                if bool(entry.can_handle(worker)):
                    return entry
            except Exception:
                continue
        return None
