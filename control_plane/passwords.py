"""Password hashing for builders and for the people who log into an app.

argon2id, per Readme.md section 16. The sidecar never sees a hash and never
does the comparison: it asks here and believes the answer, so this is the only
place in the product where a password is checked.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError):
        return False
