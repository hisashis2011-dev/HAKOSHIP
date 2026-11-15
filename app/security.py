import secrets
import time
from collections import defaultdict

import bcrypt


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode()[:72], bcrypt.gensalt()).decode()


def verify_password(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode()[:72], hashed.encode())
    except ValueError:
        return False


def temp_password() -> str:
    alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(12))


class LoginThrottle:
    """メールアドレス+IP単位の簡易ブルートフォース対策(プロセス内メモリ)"""
    MAX_FAILS, WINDOW = 5, 300

    def __init__(self):
        self.fails: dict[str, list[float]] = defaultdict(list)

    def _recent(self, key):
        t = time.time()
        self.fails[key] = [x for x in self.fails[key] if t - x < self.WINDOW]
        return self.fails[key]

    def blocked(self, key) -> bool:
        return len(self._recent(key)) >= self.MAX_FAILS

    def fail(self, key):
        self._recent(key).append(time.time())

    def reset(self, key):
        self.fails.pop(key, None)
