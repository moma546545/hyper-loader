import base64

import pytest

from core.background_workers import _canonical_manifest_payload, _verify_manifest_signature


pytest.importorskip("cryptography")

from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding, rsa  # noqa: E402


def _generate_key_pair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_key, public_pem.decode("ascii")


def test_manifest_signature_verification_roundtrip():
    private_key, public_pem = _generate_key_pair()

    manifest = {
        "version": "1.2.3",
        "notes": "Test build",
        "windows": {
            "url": "https://example.com/app-win.zip",
            "sha256": "0" * 64,
        },
    }

    payload = _canonical_manifest_payload(manifest)
    signature = private_key.sign(payload, padding.PKCS1v15(), hashes.SHA256())
    signed_manifest = dict(manifest)
    signed_manifest["signature"] = base64.b64encode(signature).decode("ascii")

    ok, err = _verify_manifest_signature(signed_manifest, public_pem)
    assert ok, f"expected signature to verify, got error: {err}"


def test_manifest_signature_verification_detects_tampering():
    private_key, public_pem = _generate_key_pair()

    manifest = {
        "version": "1.2.3",
        "notes": "Test build",
        "windows": {
            "url": "https://example.com/app-win.zip",
            "sha256": "0" * 64,
        },
    }

    payload = _canonical_manifest_payload(manifest)
    signature = private_key.sign(payload, padding.PKCS1v15(), hashes.SHA256())
    signed_manifest = dict(manifest)
    signed_manifest["signature"] = base64.b64encode(signature).decode("ascii")

    tampered = dict(signed_manifest)
    tampered["version"] = "9.9.9"

    ok, _ = _verify_manifest_signature(tampered, public_pem)
    assert not ok
