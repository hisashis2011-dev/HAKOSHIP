import csv
import io

from .conftest import login, csrf, jpeg, form
from app.db import SessionLocal
from app.models import Expense


def create(c, color=(200, 10, 10), **over):
    t = csrf(c, "/expenses/new")
    r = c.post("/expenses/new", data={**form(**over), "csrf_token": t},
               files={"receipt": ("r.jpg", jpeg(color), "image/jpeg")})
    return r


def decide(c, eid, decision, comment=""):
    t = csrf(c, "/")
    return c.post(f"/expenses/{eid}/decide", data={"decision": decision, "comment": comment, "csrf_token": t})


def test_full_flow_and_csv():
    emp, mgr, acc = login("emp@x.jp"), login("mgr@x.jp"), login("acc@x.jp")
    r = create(emp)
    assert r.status_code == 303 and r.headers["location"] == "/expenses/1"
    with SessionLocal() as db:
        e = db.get(Expense, 1)
        assert (e.status, e.amount, e.property_code) == ("pending_manager", 3980, "A-001")

    assert "コインパーキング" in mgr.get("/approvals").text
    assert "コインパーキング" not in acc.get("/approvals").text  # 経理は上長ステップでは出ない
    assert decide(acc, 1, "approve").status_code == 303  # 権限なし→エラーflashで戻る
    with SessionLocal() as db:
        assert db.get(Expense, 1).status == "pending_manager"

    decide(mgr, 1, "approve")
    assert "コインパーキング" in acc.get("/approvals").text
    decide(acc, 1, "approve")
    with SessionLocal() as db:
        assert db.get(Expense, 1).status == "approved"

    t = csrf(acc, "/accounting")
    acc.post("/accounting/pay", data={"ids": ["1"], "csrf_token": t})
    with SessionLocal() as db:
        e = db.get(Expense, 1)
        assert e.status == "paid" and [h.action for h in e.history] == ["submit", "approve", "approve", "pay"]

    body = acc.get("/accounting/export.csv?month=2026-11").content.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(body)))
    assert rows[1][1:7] == ["2026-11-01", "Emp", "emp@x.jp", "コインパーキング", "3980", "旅費交通費"]
    assert rows[1][7] == "A-001"


def test_return_and_resubmit():
    emp, mgr = login("emp@x.jp"), login("mgr@x.jp")
    create(emp)
    assert decide(mgr, 1, "return", "").status_code == 303  # 理由なしは不可
    with SessionLocal() as db:
        assert db.get(Expense, 1).status == "pending_manager"
    decide(mgr, 1, "return", "領収書が不鮮明")
    with SessionLocal() as db:
        assert db.get(Expense, 1).status == "draft"
    assert "領収書が不鮮明" in emp.get("/expenses/1/edit").text
    t = csrf(emp, "/expenses/1/edit")
    r = emp.post("/expenses/1/edit", data={**form(), "csrf_token": t})  # 画像は既存のまま
    assert r.status_code == 303
    with SessionLocal() as db:
        assert db.get(Expense, 1).status == "pending_manager"


def test_no_approver_goes_to_accounting_and_no_self_approval():
    solo, acc = login("solo@x.jp"), login("acc@x.jp")
    create(solo)
    with SessionLocal() as db:
        assert db.get(Expense, 1).status == "pending_accounting"
    acc_exp = create(acc, color=(10, 200, 10))  # 経理自身の申請
    assert acc_exp.status_code == 303
    decide(acc, 2, "approve")
    with SessionLocal() as db:
        assert db.get(Expense, 2).status == "pending_accounting"  # 自己承認不可
    decide(acc, 1, "approve")
    with SessionLocal() as db:
        assert db.get(Expense, 1).status == "approved"


def test_validation_errors():
    emp = login("emp@x.jp")
    for over, msg in [({"property_code": "NOPE"}, "物件コード"), ({"property_code": "OLD"}, "物件コード"),
                      ({"amount": "0"}, "金額"), ({"amount": "abc"}, "金額"),
                      ({"expense_date": "2999-01-01"}, "未来"), ({"purpose": " "}, "使用目的"),
                      ({"invoice_no": "123"}, "インボイス"), ({"category": "x"}, "勘定科目")]:
        r = create(emp, **over)
        assert r.status_code == 400 and msg in r.text, (over, r.status_code)
    t = csrf(emp, "/expenses/new")
    r = emp.post("/expenses/new", data={**form(), "csrf_token": t})
    assert r.status_code == 400 and "領収書" in r.text
    r = emp.post("/expenses/new", data={**form(), "csrf_token": t}, files={"receipt": ("a.jpg", b"not an image")})
    assert r.status_code == 400
    with SessionLocal() as db:
        assert db.query(Expense).count() == 0


def test_duplicate_receipt_rejected():
    emp, solo = login("emp@x.jp"), login("solo@x.jp")
    assert create(emp).status_code == 303
    r = create(solo)  # 同一画像
    assert r.status_code == 400 and "同じ領収書" in r.text


def test_access_control():
    emp, other, mgr, acc = login("emp@x.jp"), login("other@x.jp"), login("mgr@x.jp"), login("acc@x.jp")
    create(emp)
    assert other.get("/expenses/1").status_code == 404
    assert other.get("/expenses/1/receipt").status_code == 404
    assert mgr.get("/expenses/1/receipt").status_code == 200
    assert acc.get("/expenses/1").status_code == 200
    assert other.get("/accounting").status_code == 403
    assert emp.get("/admin/users").status_code == 403
    assert decide(other, 1, "approve").status_code == 404
    # 提出後は編集不可
    assert emp.get("/expenses/1/edit").status_code == 403


def test_csrf_and_anonymous():
    anon = login("emp@x.jp")
    r = anon.post("/expenses/new", data=form())
    assert r.status_code == 403
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app, follow_redirects=False)
    assert c.get("/").headers["location"] == "/login"
    assert c.get("/expenses/1/receipt").status_code == 303


def test_login_throttle_and_bad_password():
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app, follow_redirects=False)
    t = csrf(c, "/login")
    codes = [c.post("/login", data={"email": "emp@x.jp", "password": "bad", "csrf_token": t}).status_code
             for _ in range(6)]
    assert codes[:5] == [401] * 5 and codes[5] == 429


def test_admin_user_csv_import_and_property_master():
    adm = login("admin@x.jp")
    t = csrf(adm, "/admin/users")
    csvdata = "email,name,role,approver_email\nn1@x.jp,新人,employee,mgr@x.jp\nn2@x.jp,課長,employee,\n"
    r = adm.post("/admin/users/import", data={"csrf_token": t}, files={"file": ("u.csv", csvdata.encode(), "text/csv")})
    assert r.status_code == 303
    page = adm.get("/admin/users").text
    assert "n1@x.jp" in page and "初期パスワード" in page
    t = csrf(adm, "/admin/properties")
    adm.post("/admin/properties/import", data={"csrf_token": t}, files={"file": ("p.csv", "code,name\nb-9,新物件\n".encode(), "text/csv")})
    assert "B-9" in adm.get("/admin/properties").text


def test_csv_formula_injection_neutralized():
    emp, acc = login("solo@x.jp"), login("acc@x.jp")
    create(emp, payee="=HYPERLINK(\"http://evil\")")
    body = acc.get("/accounting/export.csv?month=2026-11").content.decode("utf-8-sig")
    assert "'=HYPERLINK" in body
