import argparse
import base64
import json
import sys
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


def _canonical_manifest_payload(manifest: dict) -> bytes:
    payload = {k: v for k, v in (manifest or {}).items() if k != "signature"}
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sign_manifest(private_key_path: Path, manifest_path: Path, output_path: Path | None) -> int:
    try:
        raw = manifest_path.read_text(encoding="utf-8")
        data = json.loads(raw or "{}")
        if not isinstance(data, dict):
            print("Manifest must be a JSON object", file=sys.stderr)
            return 1
    except Exception as exc:
        print(f"Failed to read manifest: {exc}", file=sys.stderr)
        return 1

    data.pop("signature", None)

    try:
        key_data = private_key_path.read_bytes()
        private_key = serialization.load_pem_private_key(key_data, password=None)
    except Exception as exc:
        print(f"Failed to load private key: {exc}", file=sys.stderr)
        return 1

    try:
        payload = _canonical_manifest_payload(data)
        signature = private_key.sign(payload, padding.PKCS1v15(), hashes.SHA256())
        data["signature"] = base64.b64encode(signature).decode("ascii")
    except Exception as exc:
        print(f"Failed to sign manifest: {exc}", file=sys.stderr)
        return 1

    out_json = json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2)

    if output_path is None:
        sys.stdout.write(out_json)
        if not out_json.endswith("\n"):
            sys.stdout.write("\n")
        return 0

    try:
        output_path.write_text(out_json + ("\n" if not out_json.endswith("\n") else ""), encoding="utf-8")
    except Exception as exc:
        print(f"Failed to write output manifest: {exc}", file=sys.stderr)
        return 1

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sign VidDownloader update manifest with RSA private key")
    parser.add_argument("--key", required=True, help="Path to RSA private key in PEM format")
    parser.add_argument("--manifest", required=True, help="Path to manifest JSON file to sign")
    parser.add_argument(
        "--out",
        help="Path to write signed manifest (defaults to stdout). "
        "If omitted, the signed JSON is printed to stdout.",
    )

    args = parser.parse_args(argv)
    key_path = Path(args.key).expanduser().resolve()
    manifest_path = Path(args.manifest).expanduser().resolve()
    out_path = Path(args.out).expanduser().resolve() if args.out else None

    if not key_path.is_file():
        print(f"Private key not found: {key_path}", file=sys.stderr)
        return 1
    if not manifest_path.is_file():
        print(f"Manifest file not found: {manifest_path}", file=sys.stderr)
        return 1

    return sign_manifest(key_path, manifest_path, out_path)


if __name__ == "__main__":
    raise SystemExit(main())

