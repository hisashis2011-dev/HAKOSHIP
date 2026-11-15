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


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(100))
    password_hash: Mapped[str] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(String(20), default=EMPLOYEE)
    approver_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    approver: Mapped["User | None"] = relationship(remote_side="User.id")

    @property
    def is_accounting(self) -> bool:
        return self.role in ACCOUNTING_ROLES

    @property
    def is_admin(self) -> bool:
        return self.role == ADMIN


class Property(Base):
    """物件マスタ"""
    __tablename__ = "properties"
    code: Mapped[str] = mapped_column(String(30), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Expense(Base):
    __tablename__ = "expenses"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(String(30), default=DRAFT, index=True)

    expense_date: Mapped[date] = mapped_column(Date, index=True)
    amount: Mapped[int] = mapped_column(Integer)
    payee: Mapped[str] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(50))
    purpose: Mapped[str] = mapped_column(Text)
    property_code: Mapped[str] = mapped_column(ForeignKey("properties.code"))
    invoice_no: Mapped[str | None] = mapped_column(String(14), nullable=True)  # 適格請求書発行事業者番号 T+13桁

    receipt_path: Mapped[str | None] = mapped_column(String(300), nullable=True)
    receipt_mime: Mapped[str | None] = mapped_column(String(50), nullable=True)
    receipt_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    receipt_uploaded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    user: Mapped[User] = relationship(foreign_keys=[user_id])
    property: Mapped[Property] = relationship()
    history: Mapped[list["ApprovalLog"]] = relationship(
        order_by="ApprovalLog.id", back_populates="expense", cascade="all, delete-orphan")


class ApprovalLog(Base):
    """申請・承認・差戻しの履歴(監査用。更新・削除しない)"""
    __tablename__ = "approval_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    expense_id: Mapped[int] = mapped_column(ForeignKey("expenses.id"), index=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(20))
    step: Mapped[str] = mapped_column(String(30))  # 処理時点のステータス
    comment: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    expense: Mapped[Expense] = relationship(back_populates="history")
    actor: Mapped[User] = relationship()
