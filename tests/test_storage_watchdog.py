import os
from pathlib import Path

import pytest

from core.cookie_importer import (
    decrypt_cookie_file,
    encrypt_cookie_file_inplace,
    is_encrypted_cookie_file,
)


pytestmark = pytest.mark.skipif(os.name != "nt", reason="DPAPI-based cookie encryption is Windows-only")


def test_cookie_file_encrypt_decrypt_roundtrip(tmp_path):
    plain_path = tmp_path / "cookies.txt"
    content = b"example.com\tTRUE\t/\tFALSE\t0\tname\tvalue\n"
    plain_path.write_bytes(content)

    enc_path_str = encrypt_cookie_file_inplace(str(plain_path))
    enc_path = Path(enc_path_str)

    assert enc_path.is_file()
    assert not plain_path.exists()
    assert is_encrypted_cookie_file(str(enc_path))

    decrypted = decrypt_cookie_file(str(enc_path))
    assert decrypted == content
