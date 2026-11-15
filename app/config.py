import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DATA_DIR / 'app.db'}")
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", DATA_DIR / "receipts"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# HTTPS 配下で運用する場合は COOKIE_SECURE=1
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "0") == "1"
SESSION_HOURS = int(os.getenv("SESSION_HOURS", "8"))
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "15"))
MAX_AMOUNT = int(os.getenv("MAX_AMOUNT", "10000000"))


def _secret_key() -> str:
    if os.getenv("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    f = DATA_DIR / ".secret_key"
    if not f.exists():
        f.write_text(secrets.token_hex(32))
        f.chmod(0o600)
    return f.read_text().strip()


SECRET_KEY = _secret_key()

# 勘定科目(経費区分)。必要に応じて経理と相談して変更してください。
CATEGORIES = [
    "旅費交通費", "会議費", "交際費", "消耗品費", "通信費",
    "新聞図書費", "車両費", "外注費", "福利厚生費", "雑費",
]
