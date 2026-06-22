
import os
import shutil


def resolve_probe_path(path: str) -> str:
    candidate = os.path.abspath(str(path or "").strip() or os.getcwd())
    while not os.path.exists(candidate):
        parent = os.path.dirname(candidate)
        if not parent or parent == candidate:
            return os.getcwd()
        candidate = parent
    return candidate


def free_bytes(path: str) -> int:
    target = resolve_probe_path(path)
    return int(shutil.disk_usage(target).free)


def threshold_bytes(min_free_gb: int | float) -> int:
    return int(max(0, float(min_free_gb or 0)) * 1024 * 1024 * 1024)


def has_enough_space(path: str, min_free_gb: int | float) -> tuple[bool, int, int, str]:
    target = resolve_probe_path(path)
    free = free_bytes(target)
    threshold = threshold_bytes(min_free_gb)
    return free >= threshold, free, threshold, target


def format_bytes(value: int) -> str:
    amount = float(max(0, int(value or 0)))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
        amount /= 1024
    return f"{amount:.1f} TB"



