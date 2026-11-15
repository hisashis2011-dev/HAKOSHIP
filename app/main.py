import csv
import io
import re
import secrets
import unicodedata
from datetime import date, datetime, timedelta

from fastapi import FastAPI, Depends, Request, Form, File, UploadFile, HTTPException
from fastapi.responses import RedirectResponse, FileResponse, Response, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func, or_
from sqlalchemy.orm import Session, joinedload
from starlette.middleware.sessions import SessionMiddleware

from . import workflow, receipts, ocr
from .config import (BASE_DIR, SECRET_KEY, COOKIE_SECURE, SESSION_HOURS, CATEGORIES, MAX_AMOUNT)
from .db import Base, engine, get_db
from .models import (User, Property, Expense, ApprovalLog, ROLE_LABELS, STATUS_LABELS, ACTION_LABELS,
                     EMPLOYEE, ACCOUNTING, ADMIN, DRAFT, PENDING_MANAGER, PENDING_ACCOUNTING,
                     APPROVED, PAID)
from .security import hash_password, verify_password, temp_password, LoginThrottle

app = FastAPI(title="HAKOSHIP 経費精算", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, max_age=SESSION_HOURS * 3600,
                   same_site="lax", https_only=COOKIE_SECURE, session_cookie="hakoship_session")
app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")
throttle = LoginThrottle()

Base.metadata.create_all(engine)


# ------------------------------------------------------------ helpers
class LoginRequired(Exception):
    pass


@app.exception_handler(LoginRequired)
async def _login_required(request: Request, exc):
    return RedirectResponse("/login", 303)


def csrf_token(request: Request) -> str:
    if "csrf" not in request.session:
        request.session["csrf"] = secrets.token_urlsafe(24)
    return request.session["csrf"]


async def verify_csrf(request: Request):
    form = await request.form()
    sent, expected = str(form.get("csrf_token", "")), request.session.get("csrf", "")
    if not expected or not secrets.compare_digest(sent, expected):
        raise HTTPException(403, "不正なリクエストです。画面を再読み込みしてください")


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    uid = request.session.get("uid")
    user = db.get(User, uid) if uid else None
    if not user or not user.is_active:
        request.session.clear()
        raise LoginRequired()
    request.state.todo = len(pending_for(db, user))
    return user


def accounting_user(user: User = Depends(current_user)) -> User:
    if not user.is_accounting:
        raise HTTPException(403, "経理権限が必要です")
    return user


def admin_user(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(403, "管理者権限が必要です")
    return user


def render(request: Request, name: str, user: User | None = None, status_code=200, **ctx):
    ctx.update(user=user, todo=getattr(request.state, 'todo', 0), csrf=csrf_token(request), flashes=request.session.pop("flash", []),
               STATUS_LABELS=STATUS_LABELS, ROLE_LABELS=ROLE_LABELS, ACTION_LABELS=ACTION_LABELS,
               CATEGORIES=CATEGORIES, ocr_enabled=ocr.enabled())
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def flash(request: Request, msg: str, kind="ok"):
    request.session["flash"] = request.session.get("flash", []) + [[kind, msg]]


def go(url: str):
    return RedirectResponse(url, 303)


def norm(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "").strip()


def get_expense(db: Session, eid: int, user: User) -> Expense:
    e = db.scalar(select(Expense).options(joinedload(Expense.user), joinedload(Expense.property))
                  .where(Expense.id == eid))
    if not e or not workflow.can_view(user, e):
        raise HTTPException(404, "申請が見つかりません")
    return e


def pending_for(db: Session, user: User) -> list[Expense]:
    """自分が処理すべき申請"""
    conds = []
    mgr = (Expense.status == PENDING_MANAGER) & Expense.user.has(User.approver_id == user.id)
    conds.append(mgr)
    if user.is_admin:
        conds.append(Expense.status == PENDING_MANAGER)
    if user.is_accounting:
        conds.append(Expense.status == PENDING_ACCOUNTING)
    q = (select(Expense).options(joinedload(Expense.user), joinedload(Expense.property))
         .where(or_(*conds), Expense.user_id != user.id).order_by(Expense.submitted_at))
    return list(db.scalars(q))


# ------------------------------------------------------------ auth
@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return render(request, "login.html")


@app.post("/login", dependencies=[Depends(verify_csrf)])
def login(request: Request, email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    email = norm(email).lower()
    key = f"{email}|{request.client.host if request.client else ''}"
    if throttle.blocked(key):
        return render(request, "login.html", status_code=429,
                      error="ログイン試行が多すぎます。5分ほど待ってから再度お試しください")
    user = db.scalar(select(User).where(User.email == email))
    if not user or not user.is_active or not verify_password(password, user.password_hash):
        throttle.fail(key)
        return render(request, "login.html", status_code=401, error="メールアドレスまたはパスワードが違います")
    throttle.reset(key)
    request.session.clear()
    request.session["uid"] = user.id
    return go("/")


@app.post("/logout", dependencies=[Depends(verify_csrf)])
def logout(request: Request):
    request.session.clear()
    return go("/login")


@app.get("/password", response_class=HTMLResponse)
def password_page(request: Request, user: User = Depends(current_user)):
    return render(request, "password.html", user)


@app.post("/password", dependencies=[Depends(verify_csrf)])
def password_change(request: Request, current: str = Form(...), new: str = Form(...),
                    user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not verify_password(current, user.password_hash):
        return render(request, "password.html", user, 400, error="現在のパスワードが違います")
    if len(new) < 10:
        return render(request, "password.html", user, 400, error="新しいパスワードは10文字以上にしてください")
    user.password_hash = hash_password(new)
    db.commit()
    flash(request, "パスワードを変更しました")
    return go("/")


# ------------------------------------------------------------ expenses (applicant)
@app.get("/", response_class=HTMLResponse)
def my_expenses(request: Request, status: str = "", user: User = Depends(current_user),
                db: Session = Depends(get_db)):
    q = (select(Expense).options(joinedload(Expense.property))
         .where(Expense.user_id == user.id).order_by(Expense.expense_date.desc(), Expense.id.desc()))
    if status in STATUS_LABELS:
        q = q.where(Expense.status == status)
    items = list(db.scalars(q))
    return render(request, "list.html", user, items=items, status=status)


def _properties(db):
    return list(db.scalars(select(Property).where(Property.is_active).order_by(Property.code)))


def _validate(db: Session, f: dict) -> tuple[dict, list[str]]:
    errs, c = [], {}
    try:
        c["expense_date"] = date.fromisoformat(norm(f["expense_date"]))
        if c["expense_date"] > date.today():
            errs.append("利用日に未来の日付は指定できません")
    except ValueError:
        errs.append("利用日を入力してください")
    try:
        c["amount"] = int(re.sub(r"[,円¥\s]", "", norm(f["amount"])))
        if not 1 <= c["amount"] <= MAX_AMOUNT:
            errs.append(f"金額は1〜{MAX_AMOUNT:,}円の範囲で入力してください")
    except ValueError:
        errs.append("金額は数字で入力してください")
    c["payee"] = norm(f["payee"])
    if not c["payee"]:
        errs.append("支払先(店名)を入力してください")
    c["category"] = norm(f["category"])
    if c["category"] not in CATEGORIES:
        errs.append("勘定科目を選択してください")
    c["purpose"] = f["purpose"].strip()
    if not c["purpose"]:
        errs.append("使用目的を入力してください")
    elif len(c["purpose"]) > 500:
        errs.append("使用目的は500文字以内で入力してください")
    code = norm(f["property_code"]).upper()
    p = db.get(Property, code) if code else None
    if not p or not p.is_active:
        errs.append("物件コードがマスタにありません。入力候補から選択してください")
    c["property_code"] = code
    inv = norm(f["invoice_no"]).upper()
    if inv and not re.fullmatch(r"T\d{13}", inv):
        errs.append("インボイス登録番号は「T」+数字13桁で入力してください(不明なら空欄)")
    c["invoice_no"] = inv or None
    return c, errs


async def _save_expense(request: Request, db: Session, user: User, e: Expense | None,
                        f: dict, receipt: UploadFile | None, action: str):
    new_file = bool(receipt and receipt.filename)
    data = await receipt.read() if new_file else b""
    c, errs = _validate(db, f)
    if not new_file and not (e and e.receipt_path):
        errs.append("領収書の写真またはPDFを添付してください")

    saved = None
    if new_file and not errs:
        try:
            saved = receipts.save_receipt(data)
        except receipts.ReceiptError as ex:
            errs.append(str(ex))
        else:
            dup = db.scalar(select(Expense).where(Expense.receipt_sha256 == saved[2],
                                                  Expense.id != (e.id if e else -1)))
            if dup:
                receipts.delete_receipt(saved[0])
                saved = None
                errs.append(f"同じ領収書が既に登録されています(申請 #{dup.id})")
    if errs:
        if saved:
            receipts.delete_receipt(saved[0])
        return render(request, "form.html", user, 400, e=e, f=f, errors=errs, properties=_properties(db))

    if e is None:
        e = Expense(user_id=user.id, **c)
        e.user = user
        db.add(e)
    else:
        for k, v in c.items():
            setattr(e, k, v)
    if saved:
        receipts.delete_receipt(e.receipt_path)
        e.receipt_path, e.receipt_mime, e.receipt_sha256 = saved
        e.receipt_uploaded_at = datetime.now()
    db.flush()
    if action == "submit":
        similar = db.scalar(select(func.count()).select_from(Expense).where(
            Expense.user_id == user.id, Expense.id != e.id, Expense.expense_date == e.expense_date,
            Expense.amount == e.amount, Expense.payee == e.payee))
        workflow.submit(db, e, user)
        db.commit()
        flash(request, "申請しました" + ("(同日・同額・同支払先の申請が他にもあります。重複でないかご確認ください)"
                                       if similar else ""), "warn" if similar else "ok")
    else:
        db.commit()
        flash(request, "下書きを保存しました")
    return go(f"/expenses/{e.id}")


def _blank_form():
    return {"expense_date": date.today().isoformat(), "amount": "", "payee": "", "category": "",
            "purpose": "", "property_code": "", "invoice_no": ""}


@app.get("/expenses/new", response_class=HTMLResponse)
def new_page(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return render(request, "form.html", user, e=None, f=_blank_form(), errors=[], properties=_properties(db))


@app.post("/expenses/new", dependencies=[Depends(verify_csrf)])
async def new_submit(request: Request, expense_date: str = Form(""), amount: str = Form(""),
                     payee: str = Form(""), category: str = Form(""), purpose: str = Form(""),
                     property_code: str = Form(""), invoice_no: str = Form(""),
                     action: str = Form("save"), receipt: UploadFile | None = File(None),
                     user: User = Depends(current_user), db: Session = Depends(get_db)):
    f = dict(expense_date=expense_date, amount=amount, payee=payee, category=category,
             purpose=purpose, property_code=property_code, invoice_no=invoice_no)
    return await _save_expense(request, db, user, None, f, receipt, action)


@app.post("/expenses/ocr", dependencies=[Depends(verify_csrf)])
async def ocr_receipt(request: Request, receipt: UploadFile = File(...), user: User = Depends(current_user)):
    """領収書の自動読み取り。失敗しても ok:false を返すだけで、画面側は手入力を続けられる"""
    from fastapi.concurrency import run_in_threadpool
    try:
        data, mime, _ = receipts.normalize(await receipt.read())
        fields = await run_in_threadpool(ocr.extract, data, mime)
    except (receipts.ReceiptError, ocr.OcrError):
        return {"ok": False, "fields": {}}
    return {"ok": bool(fields), "fields": fields}


@app.get("/expenses/{eid}", response_class=HTMLResponse)
def detail(eid: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    e = get_expense(db, eid, user)
    return render(request, "detail.html", user, e=e, can_act=workflow.can_act(user, e),
                  can_edit=workflow.can_edit(user, e))


@app.get("/expenses/{eid}/edit", response_class=HTMLResponse)
def edit_page(eid: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    e = get_expense(db, eid, user)
    if not workflow.can_edit(user, e):
        raise HTTPException(403, "編集できません")
    f = dict(expense_date=e.expense_date.isoformat(), amount=str(e.amount), payee=e.payee,
             category=e.category, purpose=e.purpose, property_code=e.property_code,
             invoice_no=e.invoice_no or "")
    return render(request, "form.html", user, e=e, f=f, errors=[], properties=_properties(db))


@app.post("/expenses/{eid}/edit", dependencies=[Depends(verify_csrf)])
async def edit_submit(eid: int, request: Request, expense_date: str = Form(""), amount: str = Form(""),
                      payee: str = Form(""), category: str = Form(""), purpose: str = Form(""),
                      property_code: str = Form(""), invoice_no: str = Form(""),
                      action: str = Form("save"), receipt: UploadFile | None = File(None),
                      user: User = Depends(current_user), db: Session = Depends(get_db)):
    e = get_expense(db, eid, user)
    if not workflow.can_edit(user, e):
        raise HTTPException(403, "編集できません")
    f = dict(expense_date=expense_date, amount=amount, payee=payee, category=category,
             purpose=purpose, property_code=property_code, invoice_no=invoice_no)
    return await _save_expense(request, db, user, e, f, receipt, action)


@app.post("/expenses/{eid}/submit", dependencies=[Depends(verify_csrf)])
def submit_existing(eid: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    e = get_expense(db, eid, user)
    try:
        workflow.submit(db, e, user)
    except workflow.WorkflowError as ex:
        flash(request, str(ex), "err")
    else:
        db.commit()
        flash(request, "申請しました")
    return go(f"/expenses/{eid}")


@app.post("/expenses/{eid}/delete", dependencies=[Depends(verify_csrf)])
def delete_draft(eid: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    e = get_expense(db, eid, user)
    if not workflow.can_edit(user, e) or e.history:
        raise HTTPException(403, "削除できません(一度申請された申請は削除できません)")
    receipts.delete_receipt(e.receipt_path)
    db.delete(e)
    db.commit()
    flash(request, "下書きを削除しました")
    return go("/")


@app.get("/expenses/{eid}/receipt")
def receipt_view(eid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    e = get_expense(db, eid, user)
    if not e.receipt_path:
        raise HTTPException(404)
    return FileResponse(receipts.receipt_file(e.receipt_path), media_type=e.receipt_mime,
                        headers={"Cache-Control": "private, max-age=3600",
                                 "X-Content-Type-Options": "nosniff"})


# ------------------------------------------------------------ approval
@app.get("/approvals", response_class=HTMLResponse)
def approvals(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return render(request, "approvals.html", user, items=pending_for(db, user))


@app.post("/expenses/{eid}/decide", dependencies=[Depends(verify_csrf)])
def decide(eid: int, request: Request, decision: str = Form(...), comment: str = Form(""),
           user: User = Depends(current_user), db: Session = Depends(get_db)):
    e = get_expense(db, eid, user)
    try:
        if decision == "approve":
            workflow.approve(db, e, user, comment)
            msg = "承認しました"
        elif decision == "return":
            workflow.send_back(db, e, user, comment)
            msg = "差し戻しました"
        else:
            raise HTTPException(400)
    except workflow.WorkflowError as ex:
        flash(request, str(ex), "err")
        return go(f"/expenses/{eid}")
    db.commit()
    flash(request, msg)
    return go("/approvals")


# ------------------------------------------------------------ accounting
def _month_range(month: str):
    try:
        y, m = map(int, month.split("-"))
        start = date(y, m, 1)
    except ValueError:
        start = date.today().replace(day=1)
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start, end


def _accounting_query(month: str, status: str, q: str, prop: str):
    start, end = _month_range(month)
    stmt = (select(Expense).options(joinedload(Expense.user), joinedload(Expense.property))
            .where(Expense.expense_date >= start, Expense.expense_date < end, Expense.status != DRAFT)
            .order_by(Expense.expense_date, Expense.id))
    if status in STATUS_LABELS:
        stmt = stmt.where(Expense.status == status)
    if prop:
        stmt = stmt.where(Expense.property_code == norm(prop).upper())
    if q:
        like = f"%{norm(q)}%"
        stmt = stmt.where(or_(Expense.payee.like(like), Expense.purpose.like(like),
                              Expense.user.has(User.name.like(like))))
    return stmt, start


@app.get("/accounting", response_class=HTMLResponse)
def accounting(request: Request, month: str = "", status: str = "", q: str = "", prop: str = "",
               user: User = Depends(accounting_user), db: Session = Depends(get_db)):
    month = month or date.today().strftime("%Y-%m")
    stmt, start = _accounting_query(month, status, q, prop)
    items = list(db.scalars(stmt))
    total = sum(i.amount for i in items)
    return render(request, "accounting.html", user, items=items, month=start.strftime("%Y-%m"),
                  status=status, q=q, prop=prop, total=total, properties=_properties(db))


@app.post("/accounting/pay", dependencies=[Depends(verify_csrf)])
async def accounting_pay(request: Request, user: User = Depends(accounting_user), db: Session = Depends(get_db)):
    form = await request.form()
    ids = [int(x) for x in form.getlist("ids") if str(x).isdigit()]
    n = 0
    for e in db.scalars(select(Expense).options(joinedload(Expense.user)).where(Expense.id.in_(ids))):
        try:
            workflow.mark_paid(db, e, user)
            n += 1
        except workflow.WorkflowError:
            pass
    db.commit()
    flash(request, f"{n}件を精算済にしました")
    return go(request.headers.get("referer") or "/accounting")


def _csv_safe(v) -> str:
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


@app.get("/accounting/export.csv")
def export_csv(month: str = "", status: str = "", q: str = "", prop: str = "",
               user: User = Depends(accounting_user), db: Session = Depends(get_db)):
    month = month or date.today().strftime("%Y-%m")
    stmt, start = _accounting_query(month, status, q, prop)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["申請ID", "利用日", "申請者", "社員メール", "支払先", "金額", "勘定科目", "物件コード", "物件名",
                "使用目的", "インボイス番号", "申請日時", "最終承認日時", "精算日時", "状況"])
    for e in db.scalars(stmt):
        d = lambda x: x.strftime("%Y-%m-%d %H:%M") if x else ""
        w.writerow([e.id, e.expense_date.isoformat(), *map(_csv_safe, [e.user.name, e.user.email, e.payee]),
                    e.amount, e.category, e.property_code, _csv_safe(e.property.name), _csv_safe(e.purpose),
                    e.invoice_no or "", d(e.submitted_at), d(e.approved_at), d(e.paid_at), STATUS_LABELS[e.status]])
    body = "﻿" + buf.getvalue()  # Excelで文字化けしないようBOM付きUTF-8
    return Response(body, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="expenses_{start:%Y%m}.csv"'})


# ------------------------------------------------------------ admin
@app.get("/admin/users", response_class=HTMLResponse)
def admin_users(request: Request, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    users = list(db.scalars(select(User).order_by(User.is_active.desc(), User.name)))
    return render(request, "admin_users.html", user, users=users, new_pw=request.session.pop("new_pw", None))


def _set_approver(db, u: User, approver_email: str) -> str | None:
    approver_email = norm(approver_email).lower()
    if not approver_email:
        u.approver_id = None
        return None
    a = db.scalar(select(User).where(User.email == approver_email))
    if not a:
        return f"承認者 {approver_email} が見つかりません"
    if u.id and a.id == u.id:
        return "自分自身を承認者にはできません"
    u.approver_id = a.id
    return None


@app.post("/admin/users", dependencies=[Depends(verify_csrf)])
def admin_user_add(request: Request, email: str = Form(...), name: str = Form(...), role: str = Form(EMPLOYEE),
                   approver_email: str = Form(""), user: User = Depends(admin_user), db: Session = Depends(get_db)):
    email = norm(email).lower()
    if role not in ROLE_LABELS or db.scalar(select(User).where(User.email == email)):
        flash(request, "ロールが不正、またはメールアドレスが既に登録されています", "err")
        return go("/admin/users")
    pw = temp_password()
    u = User(email=email, name=norm(name), role=role, password_hash=hash_password(pw))
    err = _set_approver(db, u, approver_email)
    if err:
        flash(request, err, "err")
        return go("/admin/users")
    db.add(u)
    db.commit()
    request.session["new_pw"] = [[u.email, pw]]
    return go("/admin/users")


@app.post("/admin/users/import", dependencies=[Depends(verify_csrf)])
async def admin_user_import(request: Request, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    """CSV一括登録。列: email,name,role(employee/accounting/admin),approver_email"""
    form = await request.form()
    up = form.get("file")
    if not up or not getattr(up, "filename", ""):
        flash(request, "CSVファイルを選択してください", "err")
        return go("/admin/users")
    text = (await up.read()).decode("utf-8-sig", errors="replace")
    rows = list(csv.DictReader(io.StringIO(text)))
    created, errors, pws = [], [], []
    for i, r in enumerate(rows, start=2):
        email = norm(r.get("email", "")).lower()
        name = norm(r.get("name", ""))
        role = norm(r.get("role", "")) or EMPLOYEE
        if not email or not name or role not in ROLE_LABELS or db.scalar(select(User).where(User.email == email)):
            errors.append(f"{i}行目: 不正または登録済み({email or '空'})")
            continue
        pw = temp_password()
        u = User(email=email, name=name, role=role, password_hash=hash_password(pw))
        db.add(u)
        db.flush()
        created.append((u, norm(r.get("approver_email", ""))))
        pws.append([email, pw])
    for u, ap in created:  # 承認者は全員登録後に解決(CSV内の順序に依存しない)
        err = _set_approver(db, u, ap)
        if err:
            errors.append(f"{u.email}: {err}")
    db.commit()
    request.session["new_pw"] = pws
    flash(request, f"{len(created)}名を登録しました" + (f"。{len(errors)}件の問題: " + " / ".join(errors[:5]) if errors else ""),
          "warn" if errors else "ok")
    return go("/admin/users")


@app.post("/admin/users/{uid}", dependencies=[Depends(verify_csrf)])
def admin_user_update(uid: int, request: Request, name: str = Form(...), role: str = Form(...),
                      approver_email: str = Form(""), is_active: str = Form(""),
                      user: User = Depends(admin_user), db: Session = Depends(get_db)):
    u = db.get(User, uid)
    if not u or role not in ROLE_LABELS:
        raise HTTPException(404)
    if u.id == user.id and (role != ADMIN or not is_active):
        flash(request, "自分自身の管理者権限や有効状態は変更できません", "err")
        return go("/admin/users")
    err = _set_approver(db, u, approver_email)
    if err:
        flash(request, err, "err")
        return go("/admin/users")
    u.name, u.role, u.is_active = norm(name), role, bool(is_active)
    db.commit()
    flash(request, f"{u.name} を更新しました")
    return go("/admin/users")


@app.post("/admin/users/{uid}/reset", dependencies=[Depends(verify_csrf)])
def admin_user_reset(uid: int, request: Request, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    u = db.get(User, uid)
    if not u:
        raise HTTPException(404)
    pw = temp_password()
    u.password_hash = hash_password(pw)
    db.commit()
    request.session["new_pw"] = [[u.email, pw]]
    return go("/admin/users")


@app.get("/admin/properties", response_class=HTMLResponse)
def admin_properties(request: Request, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    props = list(db.scalars(select(Property).order_by(Property.code)))
    return render(request, "admin_properties.html", user, props=props)


@app.post("/admin/properties", dependencies=[Depends(verify_csrf)])
def admin_property_save(request: Request, code: str = Form(...), name: str = Form(...), is_active: str = Form(""),
                        user: User = Depends(admin_user), db: Session = Depends(get_db)):
    code = norm(code).upper()
    if not re.fullmatch(r"[A-Z0-9_\-]{1,30}", code):
        flash(request, "物件コードは英数字・ハイフン・アンダースコアで30文字以内", "err")
        return go("/admin/properties")
    p = db.get(Property, code) or Property(code=code)
    p.name, p.is_active = norm(name), bool(is_active)
    db.add(p)
    db.commit()
    flash(request, f"{code} を保存しました")
    return go("/admin/properties")


@app.post("/admin/properties/import", dependencies=[Depends(verify_csrf)])
async def admin_property_import(request: Request, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    """CSV一括登録/更新。列: code,name"""
    form = await request.form()
    up = form.get("file")
    if not up or not getattr(up, "filename", ""):
        flash(request, "CSVファイルを選択してください", "err")
        return go("/admin/properties")
    text = (await up.read()).decode("utf-8-sig", errors="replace")
    n = 0
    for r in csv.DictReader(io.StringIO(text)):
        code, name = norm(r.get("code", "")).upper(), norm(r.get("name", ""))
        if re.fullmatch(r"[A-Z0-9_\-]{1,30}", code) and name:
            p = db.get(Property, code) or Property(code=code)
            p.name, p.is_active = name, True
            db.add(p)
            n += 1
    db.commit()
    flash(request, f"{n}件の物件を登録/更新しました")
    return go("/admin/properties")
