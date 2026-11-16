"""初期データ投入

  python -m app.seed --admin-email you@example.com --admin-password '...'   # 管理者を作成
  python -m app.seed --demo                                                 # デモデータ(開発用)
部門・工事マスタは管理画面(CSV一括登録可)で登録してください。
"""
import argparse

from sqlalchemy import select

from .db import Base, engine, SessionLocal
from .models import User, Department, Project, ADMIN, ACCOUNTING, EMPLOYEE
from .security import hash_password


def upsert_user(db, email, name, role, pw, approver=None, dept=None):
    u = db.scalar(select(User).where(User.email == email))
    if not u:
        u = User(email=email, name=name, role=role, password_hash=hash_password(pw))
        db.add(u)
    u.approver, u.department_code = approver, dept
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
        if a.demo:
            pw = "demo-password-1"
            for code, name in [("0", "全社"), ("1", "管理"), ("2", "本部"), ("7", "東京"), ("11", "大阪")]:
                if not db.get(Department, code):
                    db.add(Department(code=code, name=name))
            for code, name in [("A-001", "サンプル邸新築工事"), ("A-002", "サンプルビル改修"), ("COMMON", "共通")]:
                if not db.get(Project, code):
                    db.add(Project(code=code, name=name))
            db.flush()
            upsert_user(db, "admin@example.com", "管理 太郎", ADMIN, pw, dept="1")
            upsert_user(db, "keiri@example.com", "経理 花子", ACCOUNTING, pw, dept="1")
            mgr = upsert_user(db, "mgr@example.com", "上長 次郎", EMPLOYEE, pw, dept="7")
            upsert_user(db, "staff@example.com", "社員 三郎", EMPLOYEE, pw, mgr, dept="7")
            print("デモユーザー(パスワード demo-password-1): admin@ / keiri@ / mgr@ / staff@ example.com")
        db.commit()


if __name__ == "__main__":
    main()
