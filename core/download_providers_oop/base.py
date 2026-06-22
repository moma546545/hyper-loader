import abc
import threading
from typing import Any, Optional, Type

class AbstractDownloadProvider(abc.ABC):
    """
    Abstract base class for all download providers.
    A provider is responsible for fetching the file from a URL to the disk.
    """
    
    def __init__(self, task: dict, worker: Any):
        self.task = task
        self.worker = worker
        self.is_paused = False
        self.is_cancelled = False

    @classmethod
    @abc.abstractmethod
    def can_handle(cls, url: str, is_direct: bool = False) -> bool:
        """Returns True if this provider can handle the given URL."""
        pass

    @abc.abstractmethod
    def start(self) -> None:
        """Starts the download process."""
        pass

    @abc.abstractmethod
    def pause(self) -> None:
        """Pauses the download process."""
        pass

    @abc.abstractmethod
    def resume(self) -> None:
        """Resumes the download process."""
        pass

    @abc.abstractmethod
    def stop(self) -> None:
        """Stops and cancels the download process."""
        pass

class DownloadProviderRegistry:
    """Registry to discover and instantiate download providers."""
    _providers: list[Type[AbstractDownloadProvider]] = []
    _lock = threading.RLock()

    @classmethod
    def register(cls, provider_class: Type[AbstractDownloadProvider]):
        with cls._lock:
            if provider_class not in cls._providers:
                cls._providers.append(provider_class)
            
    @classmethod
    def get_provider(cls, url: str, is_direct: bool = False) -> Optional[Type[AbstractDownloadProvider]]:
        with cls._lock:
            providers = list(cls._providers)
        for provider in providers:
            if provider.can_handle(url, is_direct):
                return provider
        return None
