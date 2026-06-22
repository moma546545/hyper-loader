from .base import AbstractDownloadProvider, DownloadProviderRegistry
from .ytdlp_provider import YtDlpProvider
from .segmented_provider import SegmentedProvider

# Module-level routing registry (separate from DownloadWorker's internal registry)
_routing_registry = DownloadProviderRegistry()
_routing_registry.register(SegmentedProvider)
_routing_registry.register(YtDlpProvider)
