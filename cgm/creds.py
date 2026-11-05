"""UserBot 凭证加解密，和旧版 cybermate 的 sessions/*.enc 格式相同，旧文件可以直接拿来用。

session 泄露等于账号失守，所以只以密文存在：scrypt 派生密钥 + AES-GCM。
口令从 CGM_PASSPHRASE 读。agent 用户能读到 .enc 文件，但读不到主进程环境变量里的口令。
"""

import getpass
import json
import os
import struct
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"CGM1"
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 15, 8, 1


def passphrase(confirm: bool = False) -> str:
    p = os.environ.get("CGM_PASSPHRASE")
    if p:
        return p
    p = getpass.getpass("口令: ")
    if confirm and p != getpass.getpass("再输一次: "):
        raise SystemExit("两次不一致")
    if not p:
        raise SystemExit("口令不能为空")
    return p


def _key(pw: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P).derive(pw.encode())


def save(path: Path, payload: dict) -> None:
    pw = passphrase(confirm=True)
    salt, nonce = os.urandom(16), os.urandom(12)
    blob = json.dumps({"kind": "user", **payload}, ensure_ascii=False).encode()
    ct = AESGCM(_key(pw, salt)).encrypt(nonce, blob, MAGIC)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".enc.tmp")
    tmp.write_bytes(MAGIC + struct.pack("<BB", 1, 0) + salt + nonce + ct)
    tmp.chmod(0o600)
    tmp.replace(path)


def load(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"没有凭证 {path}，先跑 python -m cgm login")
    data = path.read_bytes()
    if data[:4] != MAGIC:
        raise SystemExit(f"{path} 不是 cybermate 凭证文件")
    salt, nonce, ct = data[6:22], data[22:34], data[34:]
    try:
        secret = json.loads(AESGCM(_key(passphrase(), salt)).decrypt(nonce, ct, MAGIC))
    except InvalidTag:
        raise SystemExit("口令不对，或者文件被改过")
    if secret.get("kind") != "user":
        raise SystemExit(f"{path} 是 {secret.get('kind')} 凭证，不是 UserBot 的")
    return secret
