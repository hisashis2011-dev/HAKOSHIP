import csv
import io
import re

import httpx
import pytest
from fastapi.testclient import TestClient

from .conftest import login, csrf, jpeg, row
from app import ocr as ocr_mod
from app.main import app, throttle
from app.db import SessionLocal
from app.models import Report, ExpenseLine


def new_report(c) -> int:
    t = csrf(c, "/")
    r = c.post("/reports/new", data={"csrf_token": t})
    assert r.status_code == 303
    return int(re.search(r"/reports/(\d+)/edit", r.headers["location"]).group(1))


def upload(c, rid, colors):
    t = csrf(c, f"/reports/{rid}/edit")
    files = [("files", (f"r{i}.jpg", jpeg(col), "image/jpeg")) for i, col in enumerate(colors)]
    return c.post(f"/reports/{rid}/receipts", data={"csrf_token": t}, files=files)


def line_ids(rid):
    with SessionLocal() as db:
        return [l.id for l in db.get(Report, rid).lines]


def save(c, rid, rows: dict, action="save", advance="0", note=""):
    t = csrf(c, f"/reports/{rid}/edit")
    data = {"csrf_token": t, "action": action, "advance": advance, "note": note}
    for lid, r in rows.items():
        for k, v in r.items():
            data[f"{k}_{lid}"] = v
    return c.post(f"/reports/{rid}/save", data=data)


def make_submitted(c, n=2, advance="0", base=0):
    rid = new_report(c)
    upload(c, rid, [(base + 10 * i, 50, 200) for i in range(n)])
    ids = line_ids(rid)
    r = save(c, rid, {i: row(amount=str(1000 * (k + 1))) for k, i in enumerate(ids)}, "submit", advance)
    assert r.status_code == 303 and r.headers["location"] == f"/reports/{rid}", r.text[:500]
    return rid


def decide(c, rid, decision, comment=""):
    t = csrf(c, "/")
    return c.post(f"/reports/{rid}/decide", data={"decision": decision, "comment": comment, "csrf_token": t})


def test_multi_receipt_upload_creates_lines_and_full_flow_csv():
    emp, mgr, acc = login("emp@x.jp"), login("mgr@x.jp"), login("acc@x.jp")
    rid = new_report(emp)
    r = upload(emp, rid, [(200, 10, 10), (10, 200, 10), (10, 10, 200)])  # 3枚まとめて
    assert r.status_code == 303
    assert len(line_ids(rid)) == 3
    ids = line_ids(rid)
    r = save(emp, rid, {ids[0]: row(amount="1,000"), ids[1]: row(amount="2000", payee="店B"),
                        ids[2]: row(amount="3000", payee="店C")}, "submit", advance="2,000")
    assert r.status_code == 303
    with SessionLocal() as db:
        rep = db.get(Report, rid)
        assert rep.status == "pending_manager"
        assert (rep.total, rep.advance_amount, rep.balance) == (6000, 2000, -4000)

    assert "精算書 #1" in mgr.get("/approvals").text and "精算書 #1" not in acc.get("/approvals").text
    decide(acc, rid, "approve")  # 権限なし → 変化なし
    with SessionLocal() as db:
        assert db.get(Report, rid).status == "pending_manager"
    decide(mgr, rid, "approve")
    decide(acc, rid, "approve")
    t = csrf(acc, "/accounting")
    acc.post("/accounting/pay", data={"ids": [str(rid)], "csrf_token": t})
    with SessionLocal() as db:
        rep = db.get(Report, rid)
        assert rep.status == "paid" and [h.action for h in rep.history] == ["submit", "approve", "approve", "pay"]

    body = acc.get("/accounting/export.csv").content.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(body)))
    assert len(rows) == 4  # ヘッダ + 3明細
    h = rows[0]
    first = dict(zip(h, rows[1]))
    assert (first["申請者"], first["部門コード"], first["工事コード"], first["支払金額"]) == ("Emp", "7", "A-001", "1000")
    assert (first["精算書合計①"], first["仮払金②"], first["差引過不足③"]) == ("6000", "2000", "-4000")
    assert "印刷" in emp.get(f"/reports/{rid}/print").text or "立替/仮払経費精算書" in emp.get(f"/reports/{rid}/print").text


def test_validation_blocks_submit_but_draft_saves():
    emp = login("emp@x.jp")
    rid = new_report(emp)
    upload(emp, rid, [(1, 2, 3)])
    (lid,) = line_ids(rid)
    # 下書きは未入力でも保存可
    assert save(emp, rid, {lid: row(amount="", payee="", purpose="", project="")}, "save").status_code == 303
    for over, msg in [({"project": "NOPE"}, "工事コード"), ({"project": "OLD"}, "工事コード"), ({"amount": "0"}, "金額"),
                      ({"amount": "abc"}, "金額"), ({"date": "2999-01-01"}, "未来"), ({"purpose": " "}, "支払内容"),
                      ({"invoice": "123"}, "インボイス"), ({"dept": "OLD"}, "部門コード"), ({"dept": ""}, "部門コード"),
                      ({"payee": ""}, "支払先")]:
        r = save(emp, rid, {lid: row(**over)}, "submit")
        assert r.status_code == 400 and msg in r.text, (over, r.status_code)
    with SessionLocal() as db:
        assert db.get(Report, rid).status == "draft"
    # 明細ゼロでは申請不可
    rid2 = new_report(emp)
    assert save(emp, rid2, {}, "submit").status_code == 400


def test_duplicate_and_bad_files_are_skipped():
    emp, solo = login("emp@x.jp"), login("solo@x.jp")
    rid = new_report(emp)
    upload(emp, rid, [(9, 9, 9), (9, 9, 9)])  # 同一画像2枚 → 1行のみ
    assert len(line_ids(rid)) == 1
    rid2 = new_report(solo)
    upload(solo, rid2, [(9, 9, 9)])  # 他人でも同じ領収書は不可
    assert len(line_ids(rid2)) == 0
    t = csrf(solo, f"/reports/{rid2}/edit")
    solo.post(f"/reports/{rid2}/receipts", data={"csrf_token": t}, files=[("files", ("a.jpg", b"junk", "image/jpeg"))])
    assert len(line_ids(rid2)) == 0


def test_return_edit_resubmit_and_row_ops():
    emp, mgr = login("emp@x.jp"), login("mgr@x.jp")
    rid = make_submitted(emp, 1)
    assert decide(mgr, rid, "return", "").status_code == 303
    with SessionLocal() as db:
        assert db.get(Report, rid).status == "pending_manager"  # 理由なし不可
    decide(mgr, rid, "return", "領収書が不鮮明")
    assert "領収書が不鮮明" in emp.get(f"/reports/{rid}/edit").text
    (lid,) = line_ids(rid)
    save(emp, rid, {lid: row()}, "addrow")  # 領収書なし行を追加
    assert len(line_ids(rid)) == 2
    new = line_ids(rid)[1]
    save(emp, rid, {lid: row(), new: row()}, f"delete:{lid}")
    assert line_ids(rid) == [new]
    r = save(emp, rid, {new: row(amount="500", purpose="電車代")}, "submit")
    assert r.status_code == 303
    with SessionLocal() as db:
        rep = db.get(Report, rid)
        assert rep.status == "pending_manager" and rep.lines[0].no_receipt


def test_no_approver_goes_to_accounting_and_no_self_approval():
    solo, acc = login("solo@x.jp"), login("acc@x.jp")
    r1 = make_submitted(solo, 1, base=100)
    with SessionLocal() as db:
        assert db.get(Report, r1).status == "pending_accounting"
    r2 = make_submitted(acc, 1, base=150)  # 経理自身の精算書
    decide(acc, r2, "approve")
    with SessionLocal() as db:
        assert db.get(Report, r2).status == "pending_accounting"  # 自己承認不可
    decide(acc, r1, "approve")
    with SessionLocal() as db:
        assert db.get(Report, r1).status == "approved"


def test_access_control():
    emp, other, mgr, acc = login("emp@x.jp"), login("other@x.jp"), login("mgr@x.jp"), login("acc@x.jp")
    rid = make_submitted(emp, 1)
    (lid,) = line_ids(rid)
    assert other.get(f"/reports/{rid}").status_code == 404
    assert other.get(f"/lines/{lid}/receipt").status_code == 404
    assert mgr.get(f"/lines/{lid}/receipt").status_code == 200
    assert acc.get(f"/reports/{rid}").status_code == 200
    assert other.get("/accounting").status_code == 403 and emp.get("/admin/users").status_code == 403
    assert decide(other, rid, "approve").status_code == 404
    assert emp.get(f"/reports/{rid}/edit").status_code == 403  # 提出後は編集不可
    t = csrf(other, "/")
    assert other.post(f"/reports/{rid}/receipts", data={"csrf_token": t},
                      files=[("files", ("a.jpg", jpeg(), "image/jpeg"))]).status_code == 404


def test_csrf_and_anonymous():
    emp = login("emp@x.jp")
    assert emp.post("/reports/new", data={}).status_code == 403
    c = TestClient(app, follow_redirects=False)
    assert c.get("/").headers["location"] == "/login"
    assert c.get("/lines/1/receipt").status_code == 303


def test_login_throttle():
    c = TestClient(app, follow_redirects=False)
    t = csrf(c, "/login")
    codes = [c.post("/login", data={"email": "emp@x.jp", "password": "bad", "csrf_token": t}).status_code for _ in range(6)]
    assert codes[:5] == [401] * 5 and codes[5] == 429


def test_admin_import_users_departments_projects():
    adm = login("admin@x.jp")
    t = csrf(adm, "/admin/master/departments")
    adm.post("/admin/master/departments/import", data={"csrf_token": t},
             files={"file": ("d.csv", "code,name\n11,大阪\n".encode(), "text/csv")})
    assert "大阪" in adm.get("/admin/master/departments").text
    t = csrf(adm, "/admin/master/projects")
    adm.post("/admin/master/projects/import", data={"csrf_token": t},
             files={"file": ("p.csv", "code,name\nb-9,新工事\n".encode(), "text/csv")})
    assert "B-9" in adm.get("/admin/master/projects").text
    t = csrf(adm, "/admin/users")
    csvdata = "email,name,role,approver_email,department_code\nn1@x.jp,新人,employee,mgr@x.jp,11\n"
    adm.post("/admin/users/import", data={"csrf_token": t}, files={"file": ("u.csv", csvdata.encode(), "text/csv")})
    page = adm.get("/admin/users").text
    assert "n1@x.jp" in page and "初期パスワード" in page


def test_csv_formula_injection_neutralized():
    emp, acc = login("solo@x.jp"), login("acc@x.jp")
    rid = new_report(emp)
    upload(emp, rid, [(77, 7, 7)])
    (lid,) = line_ids(rid)
    save(emp, rid, {lid: row(payee='=HYPERLINK("http://evil")')}, "submit")
    assert "'=HYPERLINK" in acc.get("/accounting/export.csv").content.decode("utf-8-sig")


# ---------------- OCR (Claude Vision) ----------------
def _mock_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _api_reply(text):
    return httpx.Response(200, json={"content": [{"type": "text", "text": text}]})


def test_ocr_extract_many_parses_and_sanitizes(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    ocr_mod.breaker.__init__()
    reply = ('```json\n{"receipts":[{"expense_date":"2026-11-03","amount":1200,"payee":"カフェ","invoice_no":"T1234567890123","location":"左上"},'
             '{"expense_date":"2999-01-01","amount":980,"payee":null,"invoice_no":"bad","location":"右下"},"junk"]}\n```')
    r = ocr_mod.extract_many(jpeg(), "image/jpeg", _mock_client(lambda req: _api_reply(reply)))
    assert r == [{"expense_date": "2026-11-03", "amount": 1200, "payee": "カフェ", "invoice_no": "T1234567890123", "location": "左上"},
                 {"amount": 980, "location": "右下"}]
    assert ocr_mod.extract_many(jpeg(), "image/jpeg", _mock_client(lambda req: _api_reply('{"receipts":[]}'))) == []
    # 前後に説明文があっても読める
    chatty = '読み取りました。\n{"receipts":[{"amount":500,"payee":"A"}]}\n以上です {参考}'
    assert ocr_mod.extract_many(jpeg(), "image/jpeg", _mock_client(lambda req: _api_reply(chatty)))[0]["amount"] == 500


def test_one_scan_with_many_receipts_makes_many_lines_sharing_one_original(monkeypatch):
    emp = login("emp@x.jp")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    ocr_mod.breaker.__init__()
    monkeypatch.setattr(ocr_mod, "extract_many", lambda d, m, client=None: [
        {"expense_date": "2026-11-15", "amount": 7920, "payee": "JR西日本", "location": "左下"},
        {"expense_date": "2026-11-15", "amount": 6540, "payee": "JR西日本", "location": "右上"},
        {"expense_date": "2026-11-15", "amount": 5900, "payee": "JR西日本", "location": "右下"}])
    rid = new_report(emp)
    page = upload(emp, rid, [(4, 4, 4)])
    with SessionLocal() as db:
        ls = db.get(Report, rid).lines
        assert [l.amount for l in ls] == [7920, 6540, 5900]
        assert len({l.receipt_path for l in ls}) == 1 and ls[1].receipt_note == "2/3件目(右上)"
        path = ls[0].receipt_path
    from app.config import UPLOAD_DIR
    assert "原本の 2/3件目(右上)" in emp.get(f"/reports/{rid}/edit").text
    # 同じ原本は再取り込みできない
    upload(emp, rid, [(4, 4, 4)])
    assert len(line_ids(rid)) == 3
    # 1行削除しても他の行が使う原本は残り、全て消すと原本も消える
    ids = line_ids(rid)
    save(emp, rid, {i: row() for i in ids}, f"delete:{ids[0]}")
    assert (UPLOAD_DIR / path).exists()
    save(emp, rid, {i: row() for i in ids[1:]}, f"delete:{ids[1]}")
    save(emp, rid, {ids[2]: row()}, f"delete:{ids[2]}")
    assert not (UPLOAD_DIR / path).exists()


def test_upload_survives_ai_failure_and_empty_result(monkeypatch):
    emp = login("emp@x.jp")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    ocr_mod.breaker.__init__()

    def boom(*a, **k):
        raise ocr_mod.OcrError("failed")
    monkeypatch.setattr(ocr_mod, "extract_many", boom)
    rid = new_report(emp)
    upload(emp, rid, [(8, 8, 8)])
    assert len(line_ids(rid)) == 1
    monkeypatch.setattr(ocr_mod, "extract_many", lambda d, m, client=None: [])
    upload(emp, rid, [(9, 9, 9)])
    assert len(line_ids(rid)) == 2  # 読み取れなくても手入力用の行はできる


def test_ocr_breaker_suspends_after_failures(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    ocr_mod.breaker.__init__()
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise httpx.ConnectError("down")
    monkeypatch.setattr(ocr_mod.httpx, "post", boom)
    for _ in range(5):
        with pytest.raises(ocr_mod.OcrError):
            ocr_mod.extract_many(jpeg(), "image/jpeg")
    assert len(calls) == 3
    ocr_mod.breaker.__init__()


def test_schema_migration_adds_missing_column():
    from sqlalchemy import text
    from app.db import engine, ensure_columns
    with engine.begin() as c:
        c.execute(text("ALTER TABLE expense_lines DROP COLUMN receipt_note"))
    ensure_columns()
    with engine.begin() as c:
        c.execute(text("SELECT receipt_note FROM expense_lines")).fetchall()
