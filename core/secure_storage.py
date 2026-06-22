import base64
import ctypes
import logging
import os
import threading
import uuid

try:
    from ctypes import wintypes
except ImportError:
    wintypes = None

try:
    import keyring
except ImportError:
    keyring = None


logger = logging.getLogger("SnapDownloader.SecureStorage")
PROTECTED_REF_PREFIX = "protected://"
KEYRING_REF_PREFIX = PROTECTED_REF_PREFIX + "keyring/"
KEYRING_SERVICE_NAME = "SnapDownloader.SecureStorage"
_protector_lock = threading.Lock()
_cached_protector = None


class WindowsDataProtector:
    class DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
        ]

    def __init__(self):
        if os.name != "nt" or wintypes is None or not hasattr(ctypes, "windll"):
            raise RuntimeError("Windows data protection is unavailable")
        self._crypt32 = ctypes.windll.crypt32
        self._kernel32 = ctypes.windll.kernel32

    def _blob_from_bytes(self, raw: bytes):
        if not raw:
            return self.DATA_BLOB(0, None), None
        buffer = ctypes.create_string_buffer(raw, len(raw))
        blob = self.DATA_BLOB(
            len(raw),
            ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
        )
        return blob, buffer

    @staticmethod
    def _bytes_from_blob(blob) -> bytes:
        if not blob.cbData or not blob.pbData:
            return b""
        return ctypes.string_at(blob.pbData, blob.cbData)

    def protect(self, raw: bytes) -> bytes:
        input_blob, input_buffer = self._blob_from_bytes(raw)
        output_blob = self.DATA_BLOB()
        try:
            ok = self._crypt32.CryptProtectData(
                ctypes.byref(input_blob),
                None,
                None,
                None,
                None,
                0,
                ctypes.byref(output_blob),
            )
            if not ok:
                raise ctypes.WinError()
            return self._bytes_from_blob(output_blob)
        finally:
            if input_buffer:
                ctypes.memset(input_buffer, 0, len(input_buffer))
            input_buffer = None
            if output_blob.pbData:
                self._kernel32.LocalFree(output_blob.pbData)

    def unprotect(self, raw: bytes) -> bytes:
        input_blob, input_buffer = self._blob_from_bytes(raw)
        output_blob = self.DATA_BLOB()
        try:
            ok = self._crypt32.CryptUnprotectData(
                ctypes.byref(input_blob),
                None,
                None,
                None,
                None,
                0,
                ctypes.byref(output_blob),
            )
            if not ok:
                raise ctypes.WinError()
            return self._bytes_from_blob(output_blob)
        finally:
            if input_buffer:
                ctypes.memset(input_buffer, 0, len(input_buffer))
            input_buffer = None
            if output_blob.pbData:
                self._kernel32.LocalFree(output_blob.pbData)


def get_windows_protector():
    global _cached_protector
    if os.name != "nt":
        return None
    with _protector_lock:
        if _cached_protector is None:
            try:
                _cached_protector = WindowsDataProtector()
            except Exception as exc:
                logger.warning(f"[SecureStorage] Windows protector unavailable: {exc}")
                _cached_protector = False
        return _cached_protector if _cached_protector is not False else None


def _get_windows_protector():
    # Backward-compatible alias for internal callers.
    return get_windows_protector()


def _protect_text_with_keyring(text: str) -> str:
    if keyring is None:
        return ""
    token_id = uuid.uuid4().hex
    try:
        keyring.set_password(KEYRING_SERVICE_NAME, token_id, text)
        return KEYRING_REF_PREFIX + token_id
    except Exception as exc:
        logger.warning(f"[SecureStorage] Keyring storage unavailable: {exc}")
        return ""


def _unprotect_text_with_keyring(reference: str) -> str:
    if keyring is None:
        return ""
    token_id = str(reference or "").strip()
    if not token_id:
        return ""
    try:
        return str(keyring.get_password(KEYRING_SERVICE_NAME, token_id) or "").strip()
    except Exception as exc:
        logger.warning(f"[SecureStorage] Failed to read keyring value: {exc}")
        return ""


def protect_bytes(raw: bytes) -> bytes:
    data = bytes(raw or b"")
    protector = get_windows_protector()
    if protector is None:
        raise RuntimeError("Windows data protection is unavailable")
    return protector.protect(data)


def unprotect_bytes(raw: bytes) -> bytes:
    data = bytes(raw or b"")
    protector = get_windows_protector()
    if protector is None:
        raise RuntimeError("Windows data protection is unavailable")
    return protector.unprotect(data)


def protect_text(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith(PROTECTED_REF_PREFIX):
        return text
    protector = get_windows_protector()
    if protector is None:
        keyring_ref = _protect_text_with_keyring(text)
        if keyring_ref:
            return keyring_ref
        return text
    try:
        raw = protector.protect(text.encode("utf-8"))
        return PROTECTED_REF_PREFIX + base64.b64encode(raw).decode("ascii")
    except Exception as exc:
        logger.warning(f"[SecureStorage] Failed to protect value: {exc}")
        return text


def unprotect_text(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if not text.startswith(PROTECTED_REF_PREFIX):
        return text
    if text.startswith(KEYRING_REF_PREFIX):
        return _unprotect_text_with_keyring(text[len(KEYRING_REF_PREFIX):])
    protector = get_windows_protector()
    if protector is None:
        return ""
    encoded = text[len(PROTECTED_REF_PREFIX):].strip()
    if not encoded:
        return ""
    try:
        raw = base64.b64decode(encoded)
        return protector.unprotect(raw).decode("utf-8", errors="replace").strip()
    except Exception as exc:
        logger.warning(f"[SecureStorage] Failed to unprotect value: {exc}")
        return ""
