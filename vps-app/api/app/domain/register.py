"""rpc への登録（rpc.load_domain_handlers が register_all(register) を呼ぶ）。"""
from __future__ import annotations

from app.domain import admin, workflow

HANDLERS = {
    "getBootstrap": admin.get_bootstrap,
    "getTabCounts": workflow.get_tab_counts,
    "getRequests": workflow.get_requests,
    "getRequestDetail": workflow.get_request_detail,
    "createRequest": workflow.create_request,
    "updateRequestDraft": workflow.update_request_draft,
    "updateRequestInReview": workflow.update_request_in_review,
    "resubmitRequest": workflow.resubmit_request,
    "approveRequest": workflow.approve_request,
    "returnRequest": workflow.return_request,
    "confirmQuote": workflow.confirm_quote,
    "confirmExpedited": workflow.confirm_expedited,
    "arrangeComplete": workflow.arrange_complete,
    "recallStep": workflow.recall_step,
    "deleteRequest": workflow.delete_request,
    "generateRequestPdf": workflow.generate_request_pdf,
    "recordPresidentDecision": workflow.record_president_decision,
    "syncDeptSupervisorsNow": admin.sync_dept_supervisors_now,
    "syncWorkersNow": admin.sync_workers_now,
    "saveApproverMaster": admin.save_approver_master,
    "saveNotificationRecipients": admin.save_notification_recipients,
    "saveSettings": admin.save_settings,
    # VPS 版で足したもの（管理者のみ）
    "saveAdminAll": admin.save_admin_all,
    "setMailRedirect": admin.set_mail_redirect,
    "clearMailRedirect": admin.clear_mail_redirect,
    "listFailedJobs": admin.list_failed_jobs,
    "retryFailedJob": admin.retry_failed_job,
}


def register_all(register) -> None:
    for name, handler in HANDLERS.items():
        register(name, handler)
