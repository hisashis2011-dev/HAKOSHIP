import asyncio
import csv
import io
import re
import secrets
import unicodedata
from datetime import date, datetime, timedelta

from fastapi import FastAPI, Depends, Request, Form, File, UploadFile, HTTPException
from fastapi.responses import RedirectResponse, FileResponse, Response, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.concurrency import run_in_threadpool
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func, or_
from sqlalchemy.orm import Session, joinedload, selectinload
from starlette.middleware.sessions import SessionMiddleware

from . import workflow, receipts, ocr
from .config import (BASE_DIR, SECRET_KEY, COOKIE_SECURE, SESSION_HOURS, MAX_AMOUNT, MAX_LINES,
                     MAX_FILES_PER_UPLOAD)
from .db import Base, engine, get_db, ensure_columns
from .models import (User, Department, Project, Report, ExpenseLine, ApprovalLog, ROLE_LABELS,
                     STATUS_LABELS, ACTION_LABELS, EMPLOYEE, ACCOUNTING, ADMIN, DRAFT, PENDING_MANAGER,
                     PENDING_ACCOUNTING, APPROVED, PAID)
from .security import hash_password, verify_password, temp_password, LoginThrottle

app = FastAPI(title="エムビーエス 経費精算", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, max_age=SESSION_HOURS * 3600,
                   same_site="lax", https_only=COOKIE_SECURE, session_cookie="mbs_session")
app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")
throttle = LoginThrottle()

Base.metadata.create_all(engine)
ensure_columns()


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
               ocr_enabled=ocr.enabled(), MAX_FILES=MAX_FILES_PER_UPLOAD)
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def flash(request: Request, msg: str, kind="ok"):
    request.session["flash"] = request.session.get("flash", []) + [[kind, msg]]


def go(url: str):
    return RedirectResponse(url, 303)


def norm(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "").strip()


def get_report(db: Session, rid: int, user: User) -> Report:
    r = db.scalar(select(Report).options(joinedload(Report.user), joinedload(Report.department),
                                         selectinload(Report.lines)).where(Report.id == rid))
    if not r or not workflow.can_view(user, r):
        raise HTTPException(404, "精算書が見つかりません")
    return r


def pending_for(db: Session, user: User) -> list[Report]:
    """自分が処理すべき精算書"""
    conds = [(Report.status == PENDING_MANAGER) & Report.user.has(User.approver_id == user.id)]
    if user.is_admin:
        conds.append(Report.status == PENDING_MANAGER)
    if user.is_accounting:
        conds.append(Report.status == PENDING_ACCOUNTING)
    q = (select(Report).options(joinedload(Report.user), selectinload(Report.lines))
         .where(or_(*conds), Report.user_id != user.id).order_by(Report.submitted_at))
    return list(db.scalars(q))


def release_receipt(db: Session, path: str | None, excluding: list):
    """他の明細が同じ原本を使っていなければ、ファイルを削除する"""
    if not path:
        return
    ex = [l.id for l in excluding]
    if not db.scalar(select(func.count()).select_from(ExpenseLine).where(
            ExpenseLine.receipt_path == path, ExpenseLine.id.not_in(ex))):
        receipts.delete_receipt(path)


def projects_active(db):
    return list(db.scalars(select(Project).where(Project.is_active).order_by(Project.code)))


def departments_active(db):
    return list(db.scalars(select(Department).where(Department.is_active).order_by(Department.code)))


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


# ------------------------------------------------------------ reports (applicant)
@app.get("/", response_class=HTMLResponse)
def my_reports(request: Request, status: str = "", user: User = Depends(current_user),
               db: Session = Depends(get_db)):
    q = (select(Report).options(selectinload(Report.lines)).where(Report.user_id == user.id)
         .order_by(Report.id.desc()))
    if status in STATUS_LABELS:
        q = q.where(Report.status == status)
    return render(request, "list.html", user, items=list(db.scalars(q)), status=status)


@app.post("/reports/new", dependencies=[Depends(verify_csrf)])
def report_new(user: User = Depends(current_user), db: Session = Depends(get_db)):
    r = Report(user_id=user.id, department_code=user.department_code)
    db.add(r)
    db.commit()
    return go(f"/reports/{r.id}/edit")


LINE_FIELDS = ("date", "dept", "project", "payee", "amount", "purpose", "invoice")


def line_row(l: ExpenseLine) -> dict:
    return {"date": l.expense_date.isoformat() if l.expense_date else "", "dept": l.department_code or "",
            "project": l.project_code or "", "payee": l.payee or "",
            "amount": str(l.amount) if l.amount is not None else "", "purpose": l.purpose or "",
            "invoice": l.invoice_no or ""}


def validate_row(db: Session, row: dict, require: bool) -> tuple[dict, list[str]]:
    """明細1行の検証。require=False(下書き保存)は入力済み項目の形式のみ、True(申請)は必須も確認"""
    errs, c = [], {}

    def need(msg):
        if require:
            errs.append(msg)

    v = norm(row["date"])
    c["expense_date"] = None
    if v:
        try:
            c["expense_date"] = date.fromisoformat(v)
            if c["expense_date"] > date.today():
                errs.append("支払日に未来の日付は指定できません")
        except ValueError:
            errs.append("支払日の形式が不正です")
    else:
        need("支払日を入力してください")

    v = re.sub(r"[,円¥\s]", "", norm(row["amount"]))
    c["amount"] = None
    if v:
        try:
            c["amount"] = int(v)
            if not 1 <= c["amount"] <= MAX_AMOUNT:
                errs.append(f"金額は1〜{MAX_AMOUNT:,}円の範囲で入力してください")
        except ValueError:
            errs.append("金額は数字で入力してください")
    else:
        need("金額を入力してください")

    c["payee"] = norm(row["payee"]) or None
    if not c["payee"]:
        need("支払先を入力してください")

    c["purpose"] = row["purpose"].strip()
    if not c["purpose"]:
        need("支払内容・支払目的を入力してください")
    elif len(c["purpose"]) > 500:
        errs.append("支払内容・支払目的は500文字以内で入力してください")

    code = norm(row["project"]).upper()
    c["project_code"] = None
    if code:
        p = db.get(Project, code)
        if not p or not p.is_active:
            errs.append(f"工事コード「{code}」がマスタにありません。候補から選択してください")
        else:
            c["project_code"] = code
    else:
        need("工事コードを入力してください")

    dcode = norm(row["dept"])
    c["department_code"] = None
    if dcode:
        d = db.get(Department, dcode)
        if not d or not d.is_active:
            errs.append(f"部門コード「{dcode}」がマスタにありません")
        else:
            c["department_code"] = dcode
    elif require and db.scalar(select(func.count()).select_from(Department).where(Department.is_active)):
        errs.append("部門コードを選択してください")

    inv = norm(row["invoice"]).upper()
    if inv and not re.fullmatch(r"T\d{13}", inv):
        errs.append("インボイス登録番号は「T」+数字13桁で入力してください(不明なら空欄)")
    c["invoice_no"] = inv if re.fullmatch(r"T\d{13}", inv) else None
    return c, errs


def render_edit(request, user, r, db, rows=None, errors=None, advance=None, note=None, status_code=200):
    rows = rows or {l.id: line_row(l) for l in r.lines}
    return render(request, "report_edit.html", user, status_code, r=r, rows=rows, errors=errors or [],
                  advance=str(r.advance_amount or 0) if advance is None else advance,
                  note=r.note if note is None else note,
                  projects=projects_active(db), departments=departments_active(db))


@app.get("/reports/{rid}/edit", response_class=HTMLResponse)
def report_edit(rid: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    r = get_report(db, rid, user)
    if not workflow.can_edit(user, r):
        raise HTTPException(403, "編集できません")
    return render_edit(request, user, r, db)


@app.post("/reports/{rid}/receipts", dependencies=[Depends(verify_csrf)])
async def report_receipts(rid: int, request: Request, receipts_files: list[UploadFile] = File(..., alias="files"),
                          user: User = Depends(current_user), db: Session = Depends(get_db)):
    """領収書を複数まとめて取り込む。1枚=1明細。AI(設定時)で日付・金額・支払先を下書き入力"""
    r = get_report(db, rid, user)
    if not workflow.can_edit(user, r):
        raise HTTPException(403, "編集できません")
    files = [f for f in receipts_files if f.filename][:MAX_FILES_PER_UPLOAD]
    room = MAX_LINES - len(r.lines)
    msgs, prepared, seen = [], [], set()
    for f in files:
        if len(prepared) >= room:
            msgs.append(f"明細は{MAX_LINES}行までです。超過分は取り込みませんでした")
            break
        try:
            out, mime, ext = receipts.normalize(await f.read())
        except receipts.ReceiptError as ex:
            msgs.append(f"{f.filename}: {ex}")
            continue
        h = receipts.sha256(out)
        if h in seen or db.scalar(select(ExpenseLine.id).where(ExpenseLine.receipt_sha256 == h)):
            msgs.append(f"{f.filename}: 登録済みの領収書です(重複のため取り込みませんでした)")
            continue
        seen.add(h)
        prepared.append((out, mime, ext, h))

    sem = asyncio.Semaphore(4)
    ai_failed = 0

    async def read_ai(out, mime):
        nonlocal ai_failed
        if not ocr.enabled():
            return None
        async with sem:
            try:
                return await run_in_threadpool(ocr.extract_many, out, mime)
            except ocr.OcrError:
                ai_failed += 1
                return None

    results = await asyncio.gather(*[read_ai(o, m) for o, m, _, _ in prepared])
    n_files, n_lines, n_ai, n_multi = len(prepared), 0, 0, 0
    now_ = datetime.now()
    for (out, mime, ext, h), found in zip(prepared, results):
        rel = receipts.store(out, ext)  # 原本は1つ。複数の領収書が写っていれば全ての行が共有する
        found = found or [{}]
        room_left = MAX_LINES - len(r.lines) - n_lines
        if len(found) > room_left:
            msgs.append(f"明細は{MAX_LINES}行までです。超過分の領収書は取り込みませんでした")
            found = found[:max(room_left, 1)]
        multi = len(found) > 1
        n_multi += multi
        for i, f in enumerate(found, 1):
            d = f.get("expense_date")
            note = f"{i}/{len(found)}件目" + (f"({f['location']})" if f.get("location") else "") if multi else None
            db.add(ExpenseLine(
                report_id=r.id, department_code=r.department_code,
                expense_date=date.fromisoformat(d) if d else None, amount=f.get("amount"),
                payee=f.get("payee"), invoice_no=f.get("invoice_no"), purpose="",
                receipt_path=rel, receipt_mime=mime, receipt_sha256=h, receipt_uploaded_at=now_,
                receipt_note=note))
            n_lines += 1
            n_ai += bool(f.get("amount") or f.get("payee") or f.get("expense_date"))
    db.commit()
    if prepared:
        head = f"{n_files}ファイルから{n_lines}件の明細を作りました" if n_lines != n_files else f"{n_files}枚を取り込みました"
        if n_ai:
            tail = f"(うち{n_ai}件はAIが自動入力" + (f"。{n_multi}ファイルは複数の領収書を検出、各行の「領収書を見る」で原本と照合" if n_multi else "") \
                   + "。必ず内容を確認してください)"
        elif ocr.key_status() == "invalid":
            tail = "。APIキーの形式が不正です(説明文のまま設定していませんか？ 半角英数字の本物のキーを設定してください)。手入力してください"
        elif not ocr.enabled():
            tail = "。AI自動読み取りは無効です(APIキー未設定)。各行を手入力してください"
        elif ai_failed:
            tail = "。AIの読み取りに失敗しました(サーバー画面のログに理由が出ています)。手入力してください"
        else:
            tail = "。AIは項目を読み取れませんでした。手入力してください"
        flash(request, head + tail, "ok" if n_ai or ocr.key_status() == "missing" else "warn")
    for m in msgs:
        flash(request, m, "warn")
    return go(f"/reports/{rid}/edit")


@app.post("/reports/{rid}/save", dependencies=[Depends(verify_csrf)])
async def report_save(rid: int, request: Request, user: User = Depends(current_user),
                      db: Session = Depends(get_db)):
    r = get_report(db, rid, user)
    if not workflow.can_edit(user, r):
        raise HTTPException(403, "編集できません")
    form = await request.form()
    action = str(form.get("action", "save"))
    submitting = action == "submit"
    rows, cleans, errs = {}, {}, []
    for i, l in enumerate(r.lines, 1):
        row = {f: str(form.get(f"{f}_{l.id}", "")) for f in LINE_FIELDS}
        rows[l.id] = row
        c, e = validate_row(db, row, require=submitting)
        cleans[l.id] = (c, bool(e))
        errs += [f"{i}行目: {m}" for m in e]
    adv_raw = re.sub(r"[,円¥\s]", "", norm(str(form.get("advance", "0")))) or "0"
    note = str(form.get("note", "")).strip()[:500]
    advance = None
    if adv_raw.isdigit() and int(adv_raw) <= MAX_AMOUNT * 10:
        advance = int(adv_raw)
    else:
        errs.append("仮払金は0以上の数字で入力してください")
    if submitting and not r.lines:
        errs.append("明細がありません。領収書を追加してください")

    structural = action.startswith("delete:") or action == "addrow"
    if errs and not structural:
        return render_edit(request, user, r, db, rows, errs, str(form.get("advance", "0")), note, 400)

    for l in r.lines:  # 保存(構造変更時は検証OKの行のみ反映)
        c, bad = cleans[l.id]
        if bad:
            continue
        for k, v in c.items():
            setattr(l, k, v)
    if advance is not None:
        r.advance_amount = advance
    r.note = note

    if action.startswith("delete:"):
        victim = next((l for l in r.lines if str(l.id) == action[7:]), None)
        if victim:
            release_receipt(db, victim.receipt_path, [victim])
            r.lines.remove(victim)
            flash(request, "明細を削除しました")
        db.commit()
        return go(f"/reports/{rid}/edit")
    if action == "addrow":
        if len(r.lines) >= MAX_LINES:
            flash(request, f"明細は{MAX_LINES}行までです", "err")
        else:
            r.lines.append(ExpenseLine(department_code=r.department_code, no_receipt=True))
        db.commit()
        return go(f"/reports/{rid}/edit")
    if submitting:
        similar = 0
        for l in r.lines:
            similar += db.scalar(select(func.count()).select_from(ExpenseLine).join(Report).where(
                Report.user_id == user.id, Report.id != r.id, Report.status != DRAFT,
                ExpenseLine.expense_date == l.expense_date, ExpenseLine.amount == l.amount,
                ExpenseLine.payee == l.payee)) or 0
        workflow.submit(db, r, user)
        db.commit()
        flash(request, "申請しました" + (f"(過去の申請に同日・同額・同支払先の明細が{similar}件あります。重複でないかご確認ください)"
                                       if similar else ""), "warn" if similar else "ok")
        return go(f"/reports/{rid}")
    db.commit()
    flash(request, "下書きを保存しました")
    return go(f"/reports/{rid}/edit")


@app.get("/reports/{rid}", response_class=HTMLResponse)
def report_detail(rid: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    r = get_report(db, rid, user)
    return render(request, "report_detail.html", user, r=r, can_act=workflow.can_act(user, r),
                  can_edit=workflow.can_edit(user, r))


@app.get("/reports/{rid}/print", response_class=HTMLResponse)
def report_print(rid: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    r = get_report(db, rid, user)
    depts = list(db.scalars(select(Department).order_by(Department.code)))
    return render(request, "report_print.html", user, r=r, depts=depts, blank_rows=max(0, 20 - len(r.lines)))


@app.post("/reports/{rid}/delete", dependencies=[Depends(verify_csrf)])
def report_delete(rid: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    r = get_report(db, rid, user)
    if not workflow.can_edit(user, r) or r.history:
        raise HTTPException(403, "削除できません(一度申請された精算書は削除できません)")
    for path in {l.receipt_path for l in r.lines}:
        release_receipt(db, path, r.lines)
    db.delete(r)
    db.commit()
    flash(request, "下書きを削除しました")
    return go("/")


@app.get("/lines/{lid}/receipt")
def receipt_view(lid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    l = db.get(ExpenseLine, lid)
    if not l or not l.receipt_path:
        raise HTTPException(404)
    get_report(db, l.report_id, user)  # 閲覧権限チェック
    return FileResponse(receipts.receipt_file(l.receipt_path), media_type=l.receipt_mime,
                        headers={"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"})


# ------------------------------------------------------------ approval
@app.get("/approvals", response_class=HTMLResponse)
def approvals(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return render(request, "approvals.html", user, items=pending_for(db, user))


@app.post("/reports/{rid}/decide", dependencies=[Depends(verify_csrf)])
def decide(rid: int, request: Request, decision: str = Form(...), comment: str = Form(""),
           user: User = Depends(current_user), db: Session = Depends(get_db)):
    r = get_report(db, rid, user)
    try:
        if decision == "approve":
            workflow.approve(db, r, user, comment)
            msg = "承認しました"
        elif decision == "return":
            workflow.send_back(db, r, user, comment)
            msg = "差し戻しました"
        else:
            raise HTTPException(400)
    except workflow.WorkflowError as ex:
        flash(request, str(ex), "err")
        return go(f"/reports/{rid}")
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


def _accounting_query(month: str, status: str, q: str, proj: str):
    start, end = _month_range(month)
    stmt = (select(Report).options(joinedload(Report.user), selectinload(Report.lines), joinedload(Report.department))
            .where(Report.submitted_at >= datetime.combine(start, datetime.min.time()),
                   Report.submitted_at < datetime.combine(end, datetime.min.time()), Report.status != DRAFT)
            .order_by(Report.submitted_at, Report.id))
    if status in STATUS_LABELS:
        stmt = stmt.where(Report.status == status)
    if proj:
        stmt = stmt.where(Report.lines.any(ExpenseLine.project_code == norm(proj).upper()))
    if q:
        like = f"%{norm(q)}%"
        stmt = stmt.where(or_(Report.user.has(User.name.like(like)),
                              Report.lines.any(or_(ExpenseLine.payee.like(like), ExpenseLine.purpose.like(like)))))
    return stmt, start


@app.get("/accounting", response_class=HTMLResponse)
def accounting(request: Request, month: str = "", status: str = "", q: str = "", proj: str = "",
               user: User = Depends(accounting_user), db: Session = Depends(get_db)):
    month = month or date.today().strftime("%Y-%m")
    stmt, start = _accounting_query(month, status, q, proj)
    items = list(db.scalars(stmt))
    return render(request, "accounting.html", user, items=items, month=start.strftime("%Y-%m"), status=status,
                  q=q, proj=proj, total=sum(i.total for i in items), projects=projects_active(db))


@app.post("/accounting/pay", dependencies=[Depends(verify_csrf)])
async def accounting_pay(request: Request, user: User = Depends(accounting_user), db: Session = Depends(get_db)):
    form = await request.form()
    ids = [int(x) for x in form.getlist("ids") if str(x).isdigit()]
    n = 0
    for r in db.scalars(select(Report).options(joinedload(Report.user)).where(Report.id.in_(ids))):
        try:
            workflow.mark_paid(db, r, user)
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
def export_csv(month: str = "", status: str = "", q: str = "", proj: str = "",
               user: User = Depends(accounting_user), db: Session = Depends(get_db)):
    """明細単位のCSV(1行=精算書の1明細)。精算書ごとの仮払金・差引過不足は各行に付記"""
    month = month or date.today().strftime("%Y-%m")
    stmt, start = _accounting_query(month, status, q, proj)
    depts = {d.code: d.name for d in db.scalars(select(Department))}
    projs = {p.code: p.name for p in db.scalars(select(Project))}
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["精算書ID", "申請日", "申請者", "社員メール", "所属部門", "行", "部門コード", "部門名", "支払日",
                "工事コード", "工事名", "支払先", "支払金額", "支払内容・支払目的", "インボイス番号", "領収書",
                "原本内の位置", "精算書合計①", "仮払金②", "差引過不足③", "最終承認日時", "精算日時", "状況"])
    d = lambda x: x.strftime("%Y-%m-%d %H:%M") if x else ""
    for r in db.scalars(stmt):
        for n, l in enumerate(r.lines, 1):
            w.writerow([r.id, d(r.submitted_at), _csv_safe(r.user.name), _csv_safe(r.user.email),
                        r.department_code or "", n, l.department_code or "", _csv_safe(depts.get(l.department_code, "")),
                        l.expense_date.isoformat() if l.expense_date else "", l.project_code or "",
                        _csv_safe(projs.get(l.project_code, "")), _csv_safe(l.payee), l.amount or 0,
                        _csv_safe(l.purpose), l.invoice_no or "", "なし" if l.no_receipt else "あり",
                        _csv_safe(l.receipt_note), r.total, r.advance_amount, r.balance, d(r.approved_at), d(r.paid_at), STATUS_LABELS[r.status]])
    return Response("\ufeff" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="mbs_expenses_{start:%Y%m}.csv"'})


# ------------------------------------------------------------ admin
@app.get("/admin/users", response_class=HTMLResponse)
def admin_users(request: Request, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    users = list(db.scalars(select(User).order_by(User.is_active.desc(), User.name)))
    return render(request, "admin_users.html", user, users=users, new_pw=request.session.pop("new_pw", None),
                  departments=departments_active(db))


def _dept_or_none(db, code: str):
    code = norm(code)
    return code if code and db.get(Department, code) else None


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
                   approver_email: str = Form(""), department_code: str = Form(""),
                   user: User = Depends(admin_user), db: Session = Depends(get_db)):
    email = norm(email).lower()
    if role not in ROLE_LABELS or db.scalar(select(User).where(User.email == email)):
        flash(request, "ロールが不正、またはメールアドレスが既に登録されています", "err")
        return go("/admin/users")
    pw = temp_password()
    u = User(email=email, name=norm(name), role=role, password_hash=hash_password(pw),
             department_code=_dept_or_none(db, department_code))
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
    """CSV一括登録。列: email,name,role(employee/accounting/admin),approver_email,department_code"""
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
        u = User(email=email, name=name, role=role, password_hash=hash_password(pw),
                 department_code=_dept_or_none(db, r.get("department_code", "")))
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
                      approver_email: str = Form(""), is_active: str = Form(""), department_code: str = Form(""),
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
    u.department_code = _dept_or_none(db, department_code)
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


MASTERS = {
    "projects": dict(model=Project, title="工事マスタ(工事コード)", label="工事", pattern=r"[A-Z0-9_\-]{1,30}"),
    "departments": dict(model=Department, title="部門マスタ(部門コード)", label="部門", pattern=r"[0-9A-Z_\-]{1,10}"),
}


def _master(kind: str) -> dict:
    if kind not in MASTERS:
        raise HTTPException(404)
    return MASTERS[kind]


@app.get("/admin/master/{kind}", response_class=HTMLResponse)
def admin_master(kind: str, request: Request, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    m = _master(kind)
    rows = list(db.scalars(select(m["model"]).order_by(m["model"].code)))
    return render(request, "admin_master.html", user, kind=kind, m=m, rows=rows)


@app.post("/admin/master/{kind}", dependencies=[Depends(verify_csrf)])
def admin_master_save(kind: str, request: Request, code: str = Form(...), name: str = Form(...),
                      is_active: str = Form(""), user: User = Depends(admin_user), db: Session = Depends(get_db)):
    m = _master(kind)
    code = norm(code).upper()
    if not re.fullmatch(m["pattern"], code):
        flash(request, "コードの形式が不正です(英数字・ハイフン・アンダースコア)", "err")
        return go(f"/admin/master/{kind}")
    row = db.get(m["model"], code) or m["model"](code=code)
    row.name, row.is_active = norm(name), bool(is_active)
    db.add(row)
    db.commit()
    flash(request, f"{code} を保存しました")
    return go(f"/admin/master/{kind}")


@app.post("/admin/master/{kind}/import", dependencies=[Depends(verify_csrf)])
async def admin_master_import(kind: str, request: Request, user: User = Depends(admin_user),
                              db: Session = Depends(get_db)):
    """CSV一括登録/更新。列: code,name"""
    m = _master(kind)
    form = await request.form()
    up = form.get("file")
    if not up or not getattr(up, "filename", ""):
        flash(request, "CSVファイルを選択してください", "err")
        return go(f"/admin/master/{kind}")
    text = (await up.read()).decode("utf-8-sig", errors="replace")
    n = 0
    for r in csv.DictReader(io.StringIO(text)):
        code, name = norm(r.get("code", "")).upper(), norm(r.get("name", ""))
        if re.fullmatch(m["pattern"], code) and name:
            row = db.get(m["model"], code) or m["model"](code=code)
            row.name, row.is_active = name, True
            db.add(row)
            n += 1
    db.commit()
    flash(request, f"{n}件を登録/更新しました")
    return go(f"/admin/master/{kind}")
