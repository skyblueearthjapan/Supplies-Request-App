import pytest
from sqlalchemy import func, select

from app import rpc
from app.models import IdempotencyKey, Setting

ME = "tanaka@lineworks-local.info"


@pytest.fixture
def logged_in(client, login_as):
    login_as(ME)
    return client


def test_exactly_22_functions_are_public():
    assert len(rpc.RPC_FUNCTIONS) == 22
    assert "runDeferredJobs" not in rpc.RPC_FUNCTIONS
    with pytest.raises(ValueError):
        rpc.register("runDeferredJobs", lambda ctx: None)


def test_unknown_function_is_404(logged_in):
    assert logged_in.post("/api/rpc/runDeferredJobs", json=[]).status_code == 404
    assert logged_in.post("/api/rpc/__init__", json=[]).status_code == 404


def test_unimplemented_function_is_501(logged_in):
    resp = logged_in.post("/api/rpc/approveRequest", json=["REQ-1"])
    assert resp.status_code == 501
    assert resp.json()["ok"] is False


def test_success_shape_and_context(logged_in):
    seen = {}

    def get_request_detail(ctx, request_id):
        seen["user"] = ctx.user
        seen["admin"] = ctx.is_admin
        return {"requestId": request_id, "n": 1}

    rpc.register("getRequestDetail", get_request_detail)
    resp = logged_in.post("/api/rpc/getRequestDetail", json=["REQ-1"])
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "result": {"requestId": "REQ-1", "n": 1}}
    assert resp.headers["cache-control"] == "no-store"
    assert seen["user"].email == ME and seen["user"].name == "tanaka" and seen["admin"] is False


def test_business_error_is_400_and_rolls_back(logged_in, db):
    def save_settings(ctx, payload):
        ctx.db.add(Setting(key="thresholdAmount", value="1"))
        ctx.db.flush()
        raise rpc.RpcError("管理者のみ実行できます。")

    rpc.register("saveSettings", save_settings)
    resp = logged_in.post("/api/rpc/saveSettings", json=[{}])
    assert resp.status_code == 400
    assert resp.json() == {"ok": False, "error": "管理者のみ実行できます。"}
    assert db.get(Setting, "thresholdAmount") is None


def test_unexpected_error_is_500_without_details(logged_in):
    def boom(ctx):
        raise KeyError("secret detail")

    rpc.register("getTabCounts", boom)
    resp = logged_in.post("/api/rpc/getTabCounts", json=[])
    assert resp.status_code == 500
    assert "secret" not in resp.text


def test_optional_args_and_wrong_arity(logged_in):
    rpc.register("approveRequest", lambda ctx, request_id, comment="", kiosk=False, return_items=None: [request_id, comment, kiosk])
    assert logged_in.post("/api/rpc/approveRequest", json=["R"]).json()["result"] == ["R", "", False]
    assert logged_in.post("/api/rpc/approveRequest", json=["R", "ok", True]).json()["result"] == ["R", "ok", True]
    assert logged_in.post("/api/rpc/approveRequest", json=[]).status_code == 400
    assert logged_in.post("/api/rpc/approveRequest", json=["R", "", False, None, "extra"]).status_code == 400


@pytest.mark.parametrize("body", [b"{}", b"\"x\"", b"not json", b"123"])
def test_body_must_be_json_array(logged_in, body):
    rpc.register("getRequests", lambda ctx, *a: None)
    resp = logged_in.post("/api/rpc/getRequests", content=body, headers={"content-type": "application/json"})
    assert resp.status_code == 400


def test_empty_body_means_no_args(logged_in):
    rpc.register("getBootstrap", lambda ctx: "ok")
    assert logged_in.post("/api/rpc/getBootstrap").json() == {"ok": True, "result": "ok"}


def test_cross_origin_is_rejected(logged_in):
    rpc.register("getBootstrap", lambda ctx: "ok")
    assert logged_in.post("/api/rpc/getBootstrap", json=[], headers={"origin": "https://evil.example.com"}).status_code == 403
    ok = logged_in.post("/api/rpc/getBootstrap", json=[], headers={"origin": "https://supplies.lineworks-local.net"})
    assert ok.status_code == 200


def test_idempotency_key_replays_first_result(logged_in, db):
    calls = []

    def create_request(ctx, payload):
        calls.append(payload)
        ctx.db.add(Setting(key=f"k{len(calls)}", value="v"))
        return {"requestId": f"REQ-{len(calls)}"}

    rpc.register("createRequest", create_request)
    h = {"X-Idempotency-Key": "abcdefgh-1234"}
    first = logged_in.post("/api/rpc/createRequest", json=[{"a": 1}], headers=h)
    second = logged_in.post("/api/rpc/createRequest", json=[{"a": 1}], headers=h)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == {"ok": True, "result": {"requestId": "REQ-1"}}
    assert len(calls) == 1
    assert db.execute(select(func.count()).select_from(Setting)).scalar() == 1
    # 別のキーなら別の操作
    third = logged_in.post("/api/rpc/createRequest", json=[{"a": 1}], headers={"X-Idempotency-Key": "abcdefgh-5678"})
    assert third.json()["result"] == {"requestId": "REQ-2"}
    # 同じキーを別の関数に使うと断る
    rpc.register("deleteRequest", lambda ctx, rid, comment="": "deleted")
    assert logged_in.post("/api/rpc/deleteRequest", json=["R"], headers=h).status_code == 400


def test_failed_call_does_not_store_key(logged_in, db):
    state = {"n": 0}

    def resubmit(ctx, rid):
        state["n"] += 1
        if state["n"] == 1:
            raise rpc.RpcError("一時的なエラー")
        return "ok"

    rpc.register("resubmitRequest", resubmit)
    h = {"X-Idempotency-Key": "retry-key-0001"}
    assert logged_in.post("/api/rpc/resubmitRequest", json=["R"], headers=h).status_code == 400
    assert logged_in.post("/api/rpc/resubmitRequest", json=["R"], headers=h).json() == {"ok": True, "result": "ok"}
    assert db.execute(select(func.count()).select_from(IdempotencyKey)).scalar() == 1


def test_bad_idempotency_key_is_400(logged_in):
    rpc.register("createRequest", lambda ctx, p: "x")
    assert logged_in.post("/api/rpc/createRequest", json=[{}], headers={"X-Idempotency-Key": "short"}).status_code == 400


def test_idempotency_is_per_user(client, login_as):
    calls = []
    rpc.register("createRequest", lambda ctx, p: calls.append(ctx.user.email) or len(calls))
    h = {"X-Idempotency-Key": "shared-key-0001"}
    login_as("a@lineworks-local.info")
    assert client.post("/api/rpc/createRequest", json=[{}], headers=h).json()["result"] == 1
    client.cookies.clear()
    login_as("b@lineworks-local.info")
    assert client.post("/api/rpc/createRequest", json=[{}], headers=h).json()["result"] == 2


def test_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200 and resp.json()["ok"] is True
