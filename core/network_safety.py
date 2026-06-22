import ipaddress
import socket
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class HostResolutionSnapshot:
    host: str
    allowed_ips: tuple[str, ...]
    allow_private: bool = False


def is_safe_resolved_ip(ip: ipaddress._BaseAddress, allow_private: bool = False) -> bool:
    if (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return allow_private
    return True


def resolve_host_ips(
    host: str,
    resolver: Callable[[str], list[str] | tuple[str, ...]],
) -> list[ipaddress._BaseAddress]:
    addresses: list[ipaddress._BaseAddress] = []
    seen: set[str] = set()
    try:
        candidates = resolver(str(host or ""))
    except Exception:
        return []
    if isinstance(candidates, str):
        iterable_candidates = [candidates]
    else:
        iterable_candidates = list(candidates or [])
    for candidate_raw in iterable_candidates:
        candidate = str(candidate_raw or "").strip()
        if not candidate or candidate in seen:
            continue
        try:
            parsed = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        seen.add(candidate)
        addresses.append(parsed)
    return addresses


def resolve_tcp_host_ips(host: str) -> list[str]:
    infos = socket.getaddrinfo(str(host or ""), None, proto=socket.IPPROTO_TCP)
    out: list[str] = []
    for info in infos:
        sockaddr = info[4] if len(info) >= 5 else ()
        if not sockaddr:
            continue
        out.append(str(sockaddr[0] or "").strip())
    return out


def is_basic_hostname(host: str) -> bool:
    normalized = str(host or "").strip().lower()
    if not normalized:
        return False
    if len(normalized) > 253:
        return False
    # Keep this validator intentionally ASCII-only; callers can pass punycode domains.
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789-.")
    if not all(ch in allowed for ch in normalized):
        return False
    if normalized.startswith(".") or normalized.endswith(".") or ".." in normalized:
        return False
    labels = normalized.split(".")
    for label in labels:
        if not label or len(label) > 63:
            return False
        if label.startswith("-") or label.endswith("-"):
            return False
    return True


def is_safe_host(
    host: str,
    *,
    allow_private: bool = False,
    resolver: Callable[[str], list[str] | tuple[str, ...]] | None = None,
    host_validator: Callable[[str], bool] | None = None,
    resolver_attempts: int = 2,
    require_consistent_resolution: bool = True,
) -> bool:
    normalized = str(host or "").strip().lower()
    if normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1].strip()
    if not normalized:
        return False
    if normalized in {"localhost", "127.0.0.1", "::1"}:
        return allow_private
    if normalized.endswith((".local", ".internal", ".localhost", ".localdomain")):
        return allow_private
    try:
        ip = ipaddress.ip_address(normalized)
        return is_safe_resolved_ip(ip, allow_private=allow_private)
    except ValueError:
        pass
    validator = host_validator if callable(host_validator) else is_basic_hostname
    if not validator(normalized):
        return False
    if resolver is None:
        return False
    attempts = max(1, int(resolver_attempts or 1))
    resolved_snapshots: list[tuple[str, ...]] = []
    for _ in range(attempts):
        resolved_ips = resolve_host_ips(normalized, resolver)
        if not resolved_ips:
            return False
        if not all(is_safe_resolved_ip(ip, allow_private=allow_private) for ip in resolved_ips):
            return False
        resolved_snapshots.append(tuple(sorted(str(ip) for ip in resolved_ips)))
    if require_consistent_resolution and len(set(resolved_snapshots)) > 1:
        return False
    return True


def resolve_safe_host_snapshot(
    host: str,
    *,
    allow_private: bool = False,
    resolver: Callable[[str], list[str] | tuple[str, ...]] | None = None,
    host_validator: Callable[[str], bool] | None = None,
    resolver_attempts: int = 2,
    require_consistent_resolution: bool = True,
) -> HostResolutionSnapshot | None:
    normalized = str(host or "").strip().lower()
    if normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1].strip()
    if not normalized:
        return None
    try:
        ip = ipaddress.ip_address(normalized)
        if not is_safe_resolved_ip(ip, allow_private=allow_private):
            return None
        return HostResolutionSnapshot(
            host=normalized,
            allowed_ips=(str(ip),),
            allow_private=allow_private,
        )
    except ValueError:
        pass
    if not is_safe_host(
        normalized,
        allow_private=allow_private,
        resolver=resolver,
        host_validator=host_validator,
        resolver_attempts=resolver_attempts,
        require_consistent_resolution=require_consistent_resolution,
    ):
        return None
    if resolver is None:
        return None
    resolved_ips = resolve_host_ips(normalized, resolver)
    allowed = tuple(sorted({str(ip) for ip in resolved_ips}))
    if not allowed:
        return None
    return HostResolutionSnapshot(
        host=normalized,
        allowed_ips=allowed,
        allow_private=allow_private,
    )


def extract_response_peer_ip(response) -> str:
    if response is None:
        return ""
    candidates = []
    fp = getattr(response, "fp", None)
    if fp is not None:
        candidates.extend(
            [
                getattr(fp, "raw", None),
                getattr(getattr(fp, "raw", None), "_sock", None),
                getattr(fp, "_sock", None),
            ]
        )
    candidates.extend(
        [
            getattr(response, "raw", None),
            getattr(getattr(response, "raw", None), "_sock", None),
            getattr(response, "_sock", None),
        ]
    )
    for candidate in candidates:
        sock = candidate
        if sock is None:
            continue
        if not hasattr(sock, "getpeername"):
            continue
        try:
            peer = sock.getpeername()
        except Exception:
            continue
        if isinstance(peer, tuple) and len(peer) >= 1:
            return str(peer[0] or "").strip()
        if isinstance(peer, str):
            return peer.strip()
    return ""


def response_matches_snapshot(response, snapshot: HostResolutionSnapshot | None) -> bool:
    if snapshot is None:
        return False
    peer_ip = extract_response_peer_ip(response)
    if not peer_ip:
        return False
    try:
        parsed = ipaddress.ip_address(peer_ip)
    except ValueError:
        return False
    if not is_safe_resolved_ip(parsed, allow_private=snapshot.allow_private):
        return False
    allowed = set(snapshot.allowed_ips or ())
    return str(parsed) in allowed
