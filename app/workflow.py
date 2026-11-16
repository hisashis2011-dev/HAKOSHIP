"""承認フローと権限判定(画面に依存しない純粋ロジック)

  下書き ─申請→ 上長承認待ち ─承認→ 経理承認待ち ─承認→ 承認済 ─精算→ 精算済
     ▲             │差戻し              │差戻し
     └─────────────┴───────────────────┘

* 上長 = 申請者に設定された approver。未設定の場合は上長ステップを飛ばし経理へ。
* 自分の申請は自分で承認できない(経理・管理者も同様)。
* 管理者は上長不在時の代理承認が可能(履歴に残る)。
"""
from .models import (
    User, Report, ApprovalLog,
    DRAFT, PENDING_MANAGER, PENDING_ACCOUNTING, APPROVED, PAID,
    SUBMIT, APPROVE, RETURN, PAY, now,
)


class WorkflowError(Exception):
    pass


def can_view(user: User, e: Report) -> bool:
    return (
        e.user_id == user.id
        or user.is_accounting
        or (e.user.approver_id == user.id)
    )


def can_edit(user: User, e: Report) -> bool:
    return e.user_id == user.id and e.status == DRAFT


def can_act(user: User, e: Report) -> bool:
    """承認/差戻しが可能か"""
    if e.user_id == user.id:
        return False
    if e.status == PENDING_MANAGER:
        return e.user.approver_id == user.id or user.is_admin
    if e.status == PENDING_ACCOUNTING:
        return user.is_accounting
    return False


def _log(db, e, actor, action, comment=""):
    db.add(ApprovalLog(report_id=e.id, actor_id=actor.id, action=action,
                       step=e.status, comment=comment.strip()))


def submit(db, e: Report, actor: User):
    if not can_edit(actor, e):
        raise WorkflowError("この申請は提出できません")
    if not e.lines:
        raise WorkflowError("明細がありません。領収書を追加してください")
    _log(db, e, actor, SUBMIT)
    e.status = PENDING_MANAGER if e.user.approver_id else PENDING_ACCOUNTING
    e.submitted_at = now()


def approve(db, e: Report, actor: User, comment=""):
    if not can_act(actor, e):
        raise WorkflowError("この申請を承認する権限がありません")
    _log(db, e, actor, APPROVE, comment)
    if e.status == PENDING_MANAGER:
        e.status = PENDING_ACCOUNTING
    else:
        e.status = APPROVED
        e.approved_at = now()


def send_back(db, e: Report, actor: User, comment: str):
    if not can_act(actor, e):
        raise WorkflowError("この申請を差し戻す権限がありません")
    if not comment.strip():
        raise WorkflowError("差戻し理由を入力してください")
    _log(db, e, actor, RETURN, comment)
    e.status = DRAFT
    e.submitted_at = None


def mark_paid(db, e: Report, actor: User):
    if not actor.is_accounting or e.status != APPROVED:
        raise WorkflowError("精算済にできません")
    _log(db, e, actor, PAY)
    e.status = PAID
    e.paid_at = now()
