import json

import pytest

from core.background_workers import _verify_manifest_signature
from sign_manifest import main as sign_manifest_main


pytest.importorskip("cryptography")

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402


def _generate_key_pair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem.decode("ascii")


def test_sign_manifest_cli_stdout_roundtrip_verifies_signature(tmp_path, capsys):
    private_pem, public_pem = _generate_key_pair()
    key_path = tmp_path / "private.pem"
    manifest_path = tmp_path / "manifest.json"
    key_path.write_bytes(private_pem)
    manifest_path.write_text(
        json.dumps(
            {
                "version": "1.2.3",
                "notes": "Signed build",
                "signature": "stale-signature",
                "windows": {
                    "url": "https://example.com/app-win.zip",
                    "sha256": "0" * 64,
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    exit_code = sign_manifest_main(
        [
            "--key",
            str(key_path),
            "--manifest",
            str(manifest_path),
        ]
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    signed_manifest = json.loads(captured.out)
    assert signed_manifest["signature"] != "stale-signature"
    ok, err = _verify_manifest_signature(signed_manifest, public_pem)
    assert ok, err


def test_sign_manifest_cli_writes_output_file(tmp_path):
    private_pem, public_pem = _generate_key_pair()
    key_path = tmp_path / "private.pem"
    manifest_path = tmp_path / "manifest.json"
    output_path = tmp_path / "signed-manifest.json"
    key_path.write_bytes(private_pem)
    manifest_path.write_text(
        json.dumps(
            {
                "version": "9.9.9",
                "notes": "Release",
                "linux": {
                    "url": "https://example.com/app-linux.tar.gz",
                    "sha256": "1" * 64,
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    exit_code = sign_manifest_main(
        [
            "--key",
            str(key_path),
            "--manifest",
            str(manifest_path),
            "--out",
            str(output_path),
        ]
    )

    assert exit_code == 0
    signed_manifest = json.loads(output_path.read_text(encoding="utf-8"))
    ok, err = _verify_manifest_signature(signed_manifest, public_pem)
    assert ok, err
