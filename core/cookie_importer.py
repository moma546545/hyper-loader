"""
core/cookie_importer.py — Automatic Browser Cookie Importer
Extracts cookies from Chrome, Edge, and Firefox for authenticated downloads.

H-05 FIX: Replaced tempfile.mktemp() with tempfile.mkstemp() for security.
H-06 FIX: Added DPAPI decryption for Chrome/Edge/Brave encrypted cookie values.
"""
import os
import sys
import json
import sqlite3
import subprocess
import tempfile
import platform
import logging
import getpass
import re
from typing import Optional
from .secure_storage import protect_bytes, unprotect_bytes

logger = logging.getLogger("SnapDownloader.CookieImporter")

SUPPORTED_BROWSERS = ["chrome", "edge", "firefox", "brave", "opera"]


def _harden_windows_file_permissions(path: str):
    """Best-effort private ACL for exported cookie files on Windows."""
    if os.name != "nt":
        return
    target = str(path or "").strip()
    if not target or not os.path.exists(target):
        return
    try:
        import win32security  # type: ignore

        current_user = getpass.getuser()
        user_sid, _, _ = win32security.LookupAccountName(None, current_user)
        dacl = win32security.ACL()
        dacl.AddAccessAllowedAce(win32security.ACL_REVISION, win32security.FILE_ALL_ACCESS, user_sid)
        try:
            system_sid, _, _ = win32security.LookupAccountName(None, "SYSTEM")
            dacl.AddAccessAllowedAce(win32security.ACL_REVISION, win32security.FILE_ALL_ACCESS, system_sid)
        except Exception:
            pass
        security_descriptor = win32security.SECURITY_DESCRIPTOR()
        security_descriptor.SetSecurityDescriptorDacl(1, dacl, 0)
        win32security.SetFileSecurity(target, win32security.DACL_SECURITY_INFORMATION, security_descriptor)
        return
    except Exception as exc:
        logger.debug(f"[Cookie Importer] pywin32 ACL hardening unavailable: {exc}")

    try:
        raw_user = os.environ.get("USERNAME") or getpass.getuser()
        username = re.sub(r"[^a-zA-Z0-9_\-\.\\\/]", "", raw_user)
        if username:
            subprocess.run(["icacls", target, "/inheritance:r"], capture_output=True, text=True, check=False)
            subprocess.run(["icacls", target, "/grant:r", f"{username}:F"], capture_output=True, text=True, check=False)
            subprocess.run(["icacls", target, "/grant:r", "SYSTEM:F"], capture_output=True, text=True, check=False)
    except Exception as exc:
        logger.debug(f"[Cookie Importer] icacls ACL hardening failed: {exc}")

def _dpapi_protect(data: bytes) -> bytes:
    if os.name != "nt":
        raise RuntimeError("DPAPI is only available on Windows")
    return protect_bytes(data)


def _dpapi_unprotect(blob: bytes) -> bytes | None:
    if os.name != "nt":
        return None
    try:
        return unprotect_bytes(blob)
    except Exception:
        return None


def _get_browser_cookie_paths() -> dict:
    """Return known cookie database paths per browser per OS."""
    home = os.path.expanduser("~")
    system = platform.system()
    paths = {}

    if system == "Windows":
        local_app = os.getenv("LOCALAPPDATA", os.path.join(home, "AppData", "Local"))
        roaming = os.getenv("APPDATA", os.path.join(home, "AppData", "Roaming"))
        paths = {
            "chrome": os.path.join(local_app, "Google", "Chrome", "User Data", "Default", "Cookies"),
            "edge":   os.path.join(local_app, "Microsoft", "Edge", "User Data", "Default", "Cookies"),
            "brave":  os.path.join(local_app, "BraveSoftware", "Brave-Browser", "User Data", "Default", "Cookies"),
            "opera":  os.path.join(roaming, "Opera Software", "Opera Stable", "Cookies"),
            "firefox": _find_firefox_cookies_win(roaming),
        }
    elif system == "Darwin":
        paths = {
            "chrome":  os.path.join(home, "Library", "Application Support", "Google", "Chrome", "Default", "Cookies"),
            "edge":    os.path.join(home, "Library", "Application Support", "Microsoft Edge", "Default", "Cookies"),
            "brave":   os.path.join(home, "Library", "Application Support", "BraveSoftware", "Brave-Browser", "Default", "Cookies"),
            "firefox": _find_firefox_cookies_mac(home),
        }
    else:  # Linux
        paths = {
            "chrome":  os.path.join(home, ".config", "google-chrome", "Default", "Cookies"),
            "chromium": os.path.join(home, ".config", "chromium", "Default", "Cookies"),
            "edge":    os.path.join(home, ".config", "microsoft-edge", "Default", "Cookies"),
            "brave":   os.path.join(home, ".config", "BraveSoftware", "Brave-Browser", "Default", "Cookies"),
            "firefox": _find_firefox_cookies_linux(home),
        }

    return {k: v for k, v in paths.items() if v and os.path.exists(v)}


def _find_firefox_cookies_win(roaming: str) -> Optional[str]:
    profiles_dir = os.path.join(roaming, "Mozilla", "Firefox", "Profiles")
    return _find_firefox_cookies_in(profiles_dir)


def _find_firefox_cookies_mac(home: str) -> Optional[str]:
    profiles_dir = os.path.join(home, "Library", "Application Support", "Firefox", "Profiles")
    return _find_firefox_cookies_in(profiles_dir)


def _find_firefox_cookies_linux(home: str) -> Optional[str]:
    profiles_dir = os.path.join(home, ".mozilla", "firefox")
    return _find_firefox_cookies_in(profiles_dir)


def _find_firefox_cookies_in(profiles_dir: str) -> Optional[str]:
    if not os.path.isdir(profiles_dir):
        return None
    for name in os.listdir(profiles_dir):
        if name.endswith(".default-release") or name.endswith(".default"):
            candidate = os.path.join(profiles_dir, name, "cookies.sqlite")
            if os.path.exists(candidate):
                return candidate
    return None


def get_available_browsers() -> list:
    """Return list of browsers with accessible cookie files."""
    return list(_get_browser_cookie_paths().keys())


def _copy_sqlite_database(src_path: str, dst_path: str):
    src_conn = None
    dst_conn = None
    try:
        src_conn = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
        dst_conn = sqlite3.connect(dst_path)
        src_conn.backup(dst_conn)
    finally:
        if dst_conn is not None:
            dst_conn.close()
        if src_conn is not None:
            src_conn.close()


def _normalize_cookie_export_output_path(output_path: str) -> str:
    target = str(output_path or "").strip()
    if not target:
        raise ValueError("Output path is required")
    normalized = os.path.abspath(target)
    basename = os.path.basename(normalized)
    if not basename or basename in {".", ".."}:
        raise ValueError("Output path must point to a file")
    if os.path.isdir(normalized):
        raise IsADirectoryError(f"Output path points to a directory: {normalized}")
    if os.path.lexists(normalized) and not os.path.isfile(normalized):
        raise ValueError(f"Output path must be a regular file: {normalized}")
    parent_dir = os.path.dirname(normalized) or os.getcwd()
    os.makedirs(parent_dir, exist_ok=True)
    return normalized


def export_cookies_to_netscape(browser: str, output_path: str, domain_filter: str = "") -> int:
    """
    Export cookies from a browser to Netscape cookie format (compatible with yt-dlp).
    Returns number of cookies exported.
    """
    output_path = _normalize_cookie_export_output_path(output_path)
    paths = _get_browser_cookie_paths()
    cookie_db = paths.get(browser.lower())
    is_chromium = browser.lower() in ("chrome", "edge", "brave", "opera", "chromium")
    encryption_key = None

    if not cookie_db:
        raise FileNotFoundError(f"Cookie database not found for browser: {browser}")

    # H-05: Use mkstemp instead of mktemp (TOCTOU vulnerability fix)
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)  # Close the file descriptor so shutil can copy to it
    _copy_sqlite_database(cookie_db, tmp)

    try:
        conn = sqlite3.connect(tmp)
        conn.row_factory = sqlite3.Row

        # Chrome/Edge/Brave schema
        is_chromium_db = True
        try:
            rows = conn.execute(
                "SELECT host_key, name, value, encrypted_value, path, expires_utc, is_secure, is_httponly FROM cookies"
            ).fetchall()
            # H-06: Try to extract Chrome encryption key for DPAPI decryption
            if is_chromium:
                encryption_key = _get_chrome_encryption_key(browser)
        except sqlite3.OperationalError:
            is_chromium_db = False
            # Firefox schema
            rows = conn.execute(
                "SELECT host, name, value, path, expiry, isSecure, isHttpOnly FROM moz_cookies"
            ).fetchall()
        conn.close()
        # Use the DB schema detection to update is_chromium
        is_chromium = is_chromium and is_chromium_db

        count = 0
        fd, tmp_out = tempfile.mkstemp(prefix="viddl_cookies_", suffix=".txt", dir=os.path.dirname(output_path) or None)
        if os.name != "nt":
            try:
                os.fchmod(fd, 0o600)
            except Exception:
                pass
        else:
            _harden_windows_file_permissions(tmp_out)
        
        f = os.fdopen(fd, "w", encoding="utf-8")
        with f:
            f.write("# Netscape HTTP Cookie File\n")
            f.write("# HTTP Cookie Export\n\n")
            for row in rows:
                row = dict(row)
                host = str(row.get("host_key") or row.get("host", ""))
                if domain_filter and domain_filter.lower() not in host.lower():
                    continue
                is_domain = host.startswith(".")
                secure = "TRUE" if (row.get("is_secure") or row.get("isSecure")) else "FALSE"
                http_only = row.get("is_httponly") or row.get("isHttpOnly", 0)
                expires = int(row.get("expires_utc") or row.get("expiry") or 0)
                # Chrome stores timestamps as microseconds since 1601-01-01
                if expires > 13000000000000000:
                    expires = (expires - 11644473600000000) // 1000000
                name = str(row.get("name", ""))
                # H-06: Try to get decrypted value, fall back to plain value
                value = str(row.get("value", ""))
                if not value and is_chromium:
                    encrypted = row.get("encrypted_value")
                    if encrypted:
                        value = _decrypt_chrome_value(encrypted, encryption_key) or ""
                path = str(row.get("path", "/"))
                if not name or not value:
                    continue  # Skip cookies with no name or value
                fields = [
                    host,
                    "TRUE" if is_domain else "FALSE",
                    path,
                    secure,
                    str(expires),
                    name,
                    value,
                ]
                f.write("\t".join(fields) + "\n")
                count += 1
                
        try:
            os.replace(tmp_out, output_path)
            if os.name == "nt":
                _harden_windows_file_permissions(output_path)
        except Exception:
            import shutil
            shutil.move(tmp_out, output_path)
            if os.name == "nt":
                _harden_windows_file_permissions(output_path)
        
        return count

    except Exception as exc:
        raise RuntimeError(f"Failed to export cookies: {exc}") from exc
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass
        try:
            if "tmp_out" in locals() and tmp_out and os.path.exists(tmp_out):
                os.remove(tmp_out)
        except Exception:
            pass


def auto_detect_and_export(output_path: str, domain: str = "youtube.com") -> tuple:
    """
    Automatically detect installed browsers and export cookies from the first available one.
    Returns (browser_name, cookie_count) or raises RuntimeError.
    """
    available = get_available_browsers()
    preferred = ["chrome", "edge", "brave", "firefox", "opera", "chromium"]

    for browser in preferred:
        if browser in available:
            try:
                count = export_cookies_to_netscape(browser, output_path, domain_filter=domain)
                if count > 0:
                    return browser, count
            except Exception as exc:
                logger.debug(f"[Cookie Importer] Auto-export failed for {browser}: {exc}")
                continue

    raise RuntimeError("No supported browsers found or all cookie exports failed.")


# ── H-06: Chrome Cookie Decryption Helpers ────────────────────────────────────

def _get_chrome_encryption_key(browser: str) -> bytes | None:
    """Extract the AES encryption key from Chrome's Local State file."""
    try:
        import base64
        home = os.path.expanduser("~")
        local_app = os.getenv("LOCALAPPDATA", os.path.join(home, "AppData", "Local"))
        
        browser_paths = {
            "chrome": os.path.join(local_app, "Google", "Chrome", "User Data", "Local State"),
            "edge": os.path.join(local_app, "Microsoft", "Edge", "User Data", "Local State"),
            "brave": os.path.join(local_app, "BraveSoftware", "Brave-Browser", "User Data", "Local State"),
            "opera": os.path.join(os.getenv("APPDATA", ""), "Opera Software", "Opera Stable", "Local State"),
        }
        
        local_state_path = browser_paths.get(browser.lower())
        if not local_state_path or not os.path.exists(local_state_path):
            return None
            
        import json
        with open(local_state_path, "r", encoding="utf-8") as f:
            local_state = json.load(f)
        
        encrypted_key_b64 = local_state.get("os_crypt", {}).get("encrypted_key")
        if not encrypted_key_b64:
            return None
            
        encrypted_key = base64.b64decode(encrypted_key_b64)
        # Remove 'DPAPI' prefix (5 bytes)
        encrypted_key = encrypted_key[5:]
        
        # Decrypt using Windows DPAPI
        if os.name == "nt":
            raw = _dpapi_unprotect(encrypted_key)
            if raw:
                return raw
        
        return None
    except Exception as exc:
        logger.debug(f"[Cookie Importer] Failed to get Chrome encryption key: {exc}")
        return None


def _decrypt_chrome_value(encrypted_value: bytes, key: bytes | None) -> str | None:
    """Decrypt a Chrome encrypted cookie value using AES-256-GCM."""
    if not encrypted_value or not key:
        return None
    try:
        # Chrome v80+ uses AES-256-GCM with 'v10' or 'v11' prefix
        prefix = encrypted_value[:3]
        if prefix in (b"v10", b"v11"):
            nonce = encrypted_value[3:15]       # 12 bytes nonce
            ciphertext = encrypted_value[15:]    # Rest is ciphertext + 16 bytes tag
            
            try:
                from cryptography.hazmat.primitives.ciphers.aead import AESGCM
                aesgcm = AESGCM(key)
                decrypted = aesgcm.decrypt(nonce, ciphertext, None)
                return decrypted.decode("utf-8", errors="replace")
            except ImportError:
                # Fallback: try with Windows DPAPI directly (older Chrome versions)
                pass
        
        # Older Chrome: DPAPI encrypted directly (no v10/v11 prefix)
        if os.name == "nt":
            raw = _dpapi_unprotect(encrypted_value)
            if raw is not None:
                return raw.decode("utf-8", errors="replace")
        
        return None
    except Exception as exc:
        logger.debug(f"[Cookie Importer] Failed to decrypt cookie value: {exc}")
        return None


COOKIE_FILE_ENC_HEADER = "VDLENC1"


def _dpapi_encrypt(data: bytes) -> bytes:
    if os.name != "nt":
        raise RuntimeError("DPAPI is only available on Windows")
    return _dpapi_protect(data)


def _dpapi_decrypt(blob: bytes) -> bytes:
    if os.name != "nt":
        raise RuntimeError("DPAPI is only available on Windows")
    raw = _dpapi_unprotect(blob)
    if raw is None:
        raise RuntimeError("DPAPI decryption failed")
    return raw


def is_encrypted_cookie_file(path: str) -> bool:
    p = str(path or "").strip()
    if not p:
        return False
    if p.lower().endswith(".enc"):
        return True
    try:
        with open(p, "rb") as f:
            head = f.read(16)
        return head.startswith((COOKIE_FILE_ENC_HEADER + "\n").encode("utf-8"))
    except Exception:
        return False


def encrypt_cookie_file_inplace(path: str) -> str:
    src = os.path.abspath(str(path or "").strip())
    if not src or not os.path.isfile(src):
        raise FileNotFoundError("Cookie file not found")
    if is_encrypted_cookie_file(src):
        return src
    dst = src + ".enc"
    import base64
    with open(src, "rb") as f:
        plain = f.read()
    blob = _dpapi_encrypt(plain)
    payload = base64.b64encode(blob)
    with open(dst, "wb") as f:
        f.write((COOKIE_FILE_ENC_HEADER + "\n").encode("utf-8"))
        f.write(payload)
        f.write(b"\n")
    try:
        os.remove(src)
    except Exception:
        pass
    return dst


def decrypt_cookie_file(path: str) -> bytes:
    src = os.path.abspath(str(path or "").strip())
    if not src or not os.path.isfile(src):
        raise FileNotFoundError("Cookie file not found")
    import base64
    with open(src, "rb") as f:
        first = f.readline()
        header = first.decode("utf-8", errors="replace").strip()
        if header != COOKIE_FILE_ENC_HEADER:
            raise ValueError("Not an encrypted cookie file")
        payload = f.read().strip()
    blob = base64.b64decode(payload)
    return _dpapi_decrypt(blob)
