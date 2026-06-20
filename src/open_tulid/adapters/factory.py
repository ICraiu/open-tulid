from __future__ import annotations

from typing import Final

from .base import AdapterBuildRequest, StorageAdapter, StorageAdapterFactory, TrackerFormat
from .obsidian import ObsidianAdapter, config_from_storage_definition
from .obsidian_format import ObsidianTrackerFormat


class ObsidianAdapterFactory:
    adapter_type = "obsidian"

    def build(self, request: AdapterBuildRequest) -> StorageAdapter:
        return ObsidianAdapter(config_from_storage_definition(
            project_id=request.project_id,
            project_root=request.project_root,
            storage=request.workflow.storage,
        ))

    def build_tracker_format(self) -> TrackerFormat:
        return ObsidianTrackerFormat()


_FACTORIES: Final[dict[str, StorageAdapterFactory]] = {
    "obsidian": ObsidianAdapterFactory(),
}


def supported_adapter_types() -> tuple[str, ...]:
    return tuple(sorted(_FACTORIES))


def default_adapter_type() -> str:
    return next(iter(_FACTORIES))


def build_storage_adapter(request: AdapterBuildRequest) -> StorageAdapter:
    factory = _FACTORIES.get(request.tracker_type)
    if factory is None:
        raise ValueError(f"Unsupported tracker.type {request.tracker_type!r}")
    storage = request.workflow.storage
    if storage is None:
        raise ValueError("workflow.storage is required to build a storage adapter")
    return factory.build(request)


def build_tracker_format(tracker_type: str) -> TrackerFormat:
    factory = _FACTORIES.get(tracker_type)
    if factory is None:
        raise ValueError(f"Unsupported tracker.type {tracker_type!r}")
    return factory.build_tracker_format()
