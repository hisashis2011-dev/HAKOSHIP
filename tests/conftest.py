import io
import os
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["DATA_DIR"] = _tmp  # app import 前に隔離DBを指定

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.main import app
from app.db import Base, engine, SessionLocal
from app.models import User, Property, ADMIN, ACCOUNTING, EMPLOYEE
from app.security import hash_password

PW = "correct-horse-battery"


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        admin = User(email="admin@x.jp", name="Admin", role=ADMIN, password_hash=hash_password(PW))
        acc = User(email="acc@x.jp", name="Acc", role=ACCOUNTING, password_hash=hash_password(PW))
        mgr = User(email="mgr@x.jp", name="Mgr", role=EMPLOYEE, password_hash=hash_password(PW))
        other = User(email="other@x.jp", name="Other", role=EMPLOYEE, password_hash=hash_password(PW))
        db.add_all([admin, acc, mgr, other]); db.flush()
        db.add_all([
            User(email="emp@x.jp", name="Emp", role=EMPLOYEE, password_hash=hash_password(PW), approver_id=mgr.id),
            User(email="solo@x.jp", name="Solo", role=EMPLOYEE, password_hash=hash_password(PW)),
            Property(code="A-001", name="Aさん邸"), Property(code="OLD", name="旧", is_active=False),
        ])
        db.commit()


def login(email) -> TestClient:
    c = TestClient(app, follow_redirects=False)
    tok = _csrf(c, "/login")
    r = c.post("/login", data={"email": email, "password": PW, "csrf_token": tok})
    assert r.status_code == 303, r.text
    return c


def _csrf(c, path):
    import re
    html = c.get(path).text
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def csrf(c, path="/"):
    return _csrf(c, path)


def jpeg(color=(200, 10, 10), size=(300, 500)) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", size, color).save(b, "JPEG")
    return b.getvalue()


def form(**over):
    d = dict(expense_date="2026-11-01", amount="3,980", payee="コインパーキング", category="旅費交通費",
             purpose="A邸 現地調査の駐車場代", property_code="a-001", invoice_no="", action="submit")
    d.update(over)
    return d
