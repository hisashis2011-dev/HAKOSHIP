from datetime import datetime, date

from sqlalchemy import ForeignKey, String, Text, Integer, Boolean, Date, DateTime
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

# ---- roles ----
EMPLOYEE, ACCOUNTING, ADMIN = "employee", "accounting", "admin"
ROLE_LABELS = {EMPLOYEE: "一般社員", ACCOUNTING: "経理", ADMIN: "管理者"}
ACCOUNTING_ROLES = {ACCOUNTING, ADMIN}

# ---- statuses ----
DRAFT = "draft"                      # 下書き / 差戻し後
PENDING_MANAGER = "pending_manager"  # 上長承認待ち
PENDING_ACCOUNTING = "pending_accounting"  # 経理承認待ち
APPROVED = "approved"                # 承認済(未払)
PAID = "paid"                        # 精算済
STATUS_LABELS = {
    DRAFT: "下書き", PENDING_MANAGER: "上長承認待ち",
    PENDING_ACCOUNTING: "経理承認待ち", APPROVED: "承認済(未精算)", PAID: "精算済",
}

# ---- approval actions ----
SUBMIT, APPROVE, RETURN, PAY = "submit", "approve", "return", "pay"
ACTION_LABELS = {SUBMIT: "申請", APPROVE: "承認", RETURN: "差戻し", PAY: "精算済にする"}


def now() -> datetime:
    return datetime.now()


class Department(Base):
    """部門マスタ(部門コード)"""
    __tablename__ = "departments"
    code: Mapped[str] = mapped_column(String(10), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Project(Base):
    """工事マスタ(工事コード)"""
    __tablename__ = "projects"
    code: Mapped[str] = mapped_column(String(30), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(100))
    password_hash: Mapped[str] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(String(20), default=EMPLOYEE)
    department_code: Mapped[str | None] = mapped_column(ForeignKey("departments.code"), nullable=True)  # 所属
    approver_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    approver: Mapped["User | None"] = relationship(remote_side="User.id")
    department: Mapped["Department | None"] = relationship()

    @property
    def is_accounting(self) -> bool:
        return self.role in ACCOUNTING_ROLES

    @property
    def is_admin(self) -> bool:
        return self.role == ADMIN


class Report(Base):
    """立替/仮払経費精算書(1通 = 複数明細)"""
    __tablename__ = "reports"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(String(30), default=DRAFT, index=True)
    department_code: Mapped[str | None] = mapped_column(ForeignKey("departments.code"), nullable=True)  # 所属(作成時点)
    advance_amount: Mapped[int] = mapped_column(Integer, default=0)  # 仮払金②
    note: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # 申請日
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    user: Mapped[User] = relationship(foreign_keys=[user_id])
    department: Mapped[Department | None] = relationship()
    lines: Mapped[list["ExpenseLine"]] = relationship(
        order_by="ExpenseLine.id", back_populates="report", cascade="all, delete-orphan")
    history: Mapped[list["ApprovalLog"]] = relationship(
        order_by="ApprovalLog.id", back_populates="report", cascade="all, delete-orphan")

    @property
    def total(self) -> int:
        """合計①"""
        return sum(l.amount or 0 for l in self.lines)

    @property
    def balance(self) -> int:
        """差引過不足額 ③=②-①。マイナス=会社が社員へ支払う額、プラス=社員が返金する額"""
        return (self.advance_amount or 0) - self.total


class ExpenseLine(Base):
    """明細(領収書1枚 = 1行)。下書き中は未入力項目を許容"""
    __tablename__ = "expense_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey("reports.id"), index=True)
    department_code: Mapped[str | None] = mapped_column(ForeignKey("departments.code"), nullable=True)
    expense_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    project_code: Mapped[str | None] = mapped_column(ForeignKey("projects.code"), nullable=True)
    payee: Mapped[str | None] = mapped_column(String(200), nullable=True)
    amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    purpose: Mapped[str] = mapped_column(Text, default="")
    invoice_no: Mapped[str | None] = mapped_column(String(14), nullable=True)  # 適格請求書発行事業者番号 T+13桁

    no_receipt: Mapped[bool] = mapped_column(Boolean, default=False)  # 領収書なし(交通費等)
    receipt_path: Mapped[str | None] = mapped_column(String(300), nullable=True)
    receipt_mime: Mapped[str | None] = mapped_column(String(50), nullable=True)
    receipt_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    receipt_uploaded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    report: Mapped[Report] = relationship(back_populates="lines")
    department: Mapped[Department | None] = relationship()
    project: Mapped[Project | None] = relationship()


class ApprovalLog(Base):
    """申請・承認・差戻しの履歴(監査用。更新・削除しない)"""
    __tablename__ = "approval_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey("reports.id"), index=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(20))
    step: Mapped[str] = mapped_column(String(30))  # 処理時点のステータス
    comment: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    report: Mapped[Report] = relationship(back_populates="history")
    actor: Mapped[User] = relationship()
