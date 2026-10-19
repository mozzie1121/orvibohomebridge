"""DES3 (ECB) 兼容层：用 Home Assistant 自带的 cryptography 实现，避免引入 pycryptodome。

仅实现上游 vendored 代码实际用到的 API：
``DES3.new(key, DES3.MODE_ECB).encrypt(data)`` / ``.decrypt(data)``
"""

from __future__ import annotations

try:  # cryptography >= 43 把 TripleDES 移到了 decrepit 命名空间
    from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
except ImportError:  # pragma: no cover - 旧版本
    from cryptography.hazmat.primitives.ciphers.algorithms import TripleDES

from cryptography.hazmat.primitives.ciphers import Cipher, modes


class _EcB:
    def __init__(self, key: bytes) -> None:
        self._cipher = Cipher(TripleDES(key), modes.ECB())

    def encrypt(self, data: bytes) -> bytes:
        op = self._cipher.encryptor()
        return op.update(data) + op.finalize()

    def decrypt(self, data: bytes) -> bytes:
        op = self._cipher.decryptor()
        return op.update(data) + op.finalize()


class _DES3:
    MODE_ECB = "ECB"

    @staticmethod
    def new(key: bytes, mode: str = "ECB") -> _EcB:
        if mode != _DES3.MODE_ECB:
            raise ValueError("only ECB is supported by this shim")
        return _EcB(key)


DES3 = _DES3
