"""初期データ投入

  python -m app.seed --admin-email you@example.com --admin-password '...'   # 管理者を作成
  python -m app.seed --demo                                                 # デモデータ(開発用)
"""
import argparse

from sqlalchemy import select

from .db import Base, engine, SessionLocal
from .models import User, Property, ADMIN, ACCOUNTING, EMPLOYEE
from .security import hash_password


def upsert_user(db, email, name, role, pw, approver=None):
    u = db.scalar(select(User).where(User.email == email))
    if not u:
        u = User(email=email, name=name, role=role, password_hash=hash_password(pw))
        db.add(u)
    u.approver = approver
    db.flush()
    return u


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--admin-email")
    ap.add_argument("--admin-password")
    ap.add_argument("--demo", action="store_true")
    a = ap.parse_args()
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        if a.admin_email:
            if not a.admin_password or len(a.admin_password) < 10:
                raise SystemExit("--admin-password は10文字以上で指定してください")
            upsert_user(db, a.admin_email.lower(), "管理者", ADMIN, a.admin_password)
            if not db.get(Property, "COMMON"):
                db.add(Property(code="COMMON", name="共通(物件に紐づかない経費)"))
        if a.demo:
            pw = "demo-password-1"
            admin = upsert_user(db, "admin@example.com", "管理 太郎", ADMIN, pw)
            acc = upsert_user(db, "keiri@example.com", "経理 花子", ACCOUNTING, pw)
            mgr = upsert_user(db, "mgr@example.com", "上長 次郎", EMPLOYEE, pw)
            upsert_user(db, "staff@example.com", "社員 三郎", EMPLOYEE, pw, mgr)
            for code, name in [("COMMON", "共通"), ("A-001", "サンプル邸新築工事"), ("A-002", "サンプルビル改修")]:
                if not db.get(Property, code):
                    db.add(Property(code=code, name=name))
            print("デモユーザー(パスワード demo-password-1): admin@ / keiri@ / mgr@ / staff@ example.com")
        db.commit()


if __name__ == "__main__":
    main()
