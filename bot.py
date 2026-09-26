"""
magicpin Vera challenge bot — HTTP server + compose() entry point.

Endpoints (challenge-testing-brief.md §2):
    POST /v1/context   store versioned category/merchant/customer/trigger context
    POST /v1/tick      decide proactive sends for the currently-active triggers
    POST /v1/reply     next move in an ongoing conversation (send / wait / end)
    GET  /v1/healthz   liveness + loaded-context counts
    GET  /v1/metadata  bot identity
    POST /v1/teardown  wipe all state at end of test (optional in the brief)

Run: uvicorn bot:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from composer import Ctx, compose, compose_full, parse_dt  # noqa: F401  (compose re-exported for bot.compose)
from conversation_handlers import new_state, respond

SCOPES = ("category", "merchant", "customer", "trigger")
MAX_ACTIONS_PER_TICK = 20
MAX_CONTEXT_BYTES = 500 * 1024
START = time.time()

app = FastAPI(title="Vera challenge bot", version="1.0.0")


class Store:
    """All bot state, in memory, guarded by one lock (FastAPI runs sync handlers in a threadpool)."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.reset()

    def reset(self) -> None:
        self.contexts: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.conversations: Dict[str, Dict[str, Any]] = {}
        self.sent_suppression: set = set()
        self.sent_kinds: set = set()               # (merchant_id, customer_id, kind) already messaged
        self.opted_out: set = set()                # merchant_ids / customer_ids that asked us to stop
        self.merchant_memory: Dict[str, Dict[str, Any]] = {}
        self.pending: Dict[str, None] = {}          # deferred trigger ids (ordered), retried on later ticks

    def get(self, scope: str, cid: Optional[str]) -> Optional[dict]:
        if not cid:
            return None
        rec = self.contexts.get((scope, cid))
        return rec["payload"] if rec else None

    def counts(self) -> Dict[str, int]:
        c = {s: 0 for s in SCOPES}
        for (scope, _cid) in self.contexts:
            c[scope] = c.get(scope, 0) + 1
        return c


store = Store()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _bad(reason: str, details: str, status: int = 400) -> JSONResponse:
    return JSONResponse(status_code=status, content={"accepted": False, "reason": reason, "details": details})


async def _json_body(request: Request) -> Tuple[Optional[dict], Optional[JSONResponse], int]:
    raw = await request.body()
    try:
        data = json.loads(raw.decode("utf-8") or "null")
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        return None, _bad("invalid_json", str(e)), len(raw)
    if not isinstance(data, dict):
        return None, _bad("invalid_body", "request body must be a JSON object"), len(raw)
    return data, None, len(raw)


# ---------------------------------------------------------------------------
# GET endpoints
# ---------------------------------------------------------------------------


@app.get("/")
def root() -> Dict[str, Any]:
    return {"service": "vera-challenge-bot", "endpoints": ["/v1/context", "/v1/tick", "/v1/reply", "/v1/healthz", "/v1/metadata"]}


@app.get("/v1/healthz")
def healthz() -> Dict[str, Any]:
    with store.lock:
        counts = store.counts()
    return {"status": "ok", "uptime_seconds": int(time.time() - START), "contexts_loaded": counts}


@app.get("/v1/metadata")
def metadata() -> Dict[str, Any]:
    members = [m.strip() for m in os.getenv("TEAM_MEMBERS", "Yugal Gahlot").split(",") if m.strip()]
    return {
        "team_name": os.getenv("TEAM_NAME", "Team Yugal"),
        "team_members": members,
        "model": os.getenv("BOT_MODEL", "deterministic-rules-v1 (no LLM)"),
        "approach": ("Deterministic 4-context composer: trigger.kind dispatch to per-kind handlers that use only facts from "
                     "category/merchant/trigger/customer (peer-stat gaps, digest citations, merchant offers, customer slots); "
                     "rule-based reply engine with auto-reply detection, intent-to-action switch, opt-out/hostile exits, "
                     "per-turn Hindi-English detection and anti-repetition."),
        "contact_email": os.getenv("CONTACT_EMAIL", "yugalgahlot05@gmail.com"),
        "version": os.getenv("BOT_VERSION", "1.0.0"),
        "submitted_at": os.getenv("SUBMITTED_AT", "2026-09-26T00:00:00Z"),
    }


# ---------------------------------------------------------------------------
# POST /v1/context
# ---------------------------------------------------------------------------


@app.post("/v1/context")
async def push_context(request: Request):
    data, err, size = await _json_body(request)
    if err:
        return err
    if size > MAX_CONTEXT_BYTES:
        return _bad("payload_too_large", f"{size} bytes > {MAX_CONTEXT_BYTES}")
    scope, cid, version, payload = data.get("scope"), data.get("context_id"), data.get("version"), data.get("payload")
    if scope not in SCOPES:
        return _bad("invalid_scope", f"scope must be one of {list(SCOPES)}")
    if not isinstance(cid, str) or not cid.strip():
        return _bad("invalid_context_id", "context_id must be a non-empty string")
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        return _bad("invalid_version", "version must be a non-negative integer")
    if not isinstance(payload, dict):
        return _bad("invalid_payload", "payload must be a JSON object")

    payload = dict(payload)
    if scope == "trigger":
        payload.setdefault("id", cid)
    elif scope == "merchant":
        payload.setdefault("merchant_id", cid)
    elif scope == "customer":
        payload.setdefault("customer_id", cid)
    elif scope == "category":
        payload.setdefault("slug", cid)

    key = (scope, cid)
    with store.lock:
        cur = store.contexts.get(key)
        if cur and cur["version"] >= version:
            return JSONResponse(status_code=409, content={"accepted": False, "reason": "stale_version",
                                                          "current_version": cur["version"]})
        stored_at = _now_iso()
        store.contexts[key] = {"version": version, "payload": payload, "stored_at": stored_at,
                               "delivered_at": data.get("delivered_at")}
    return {"accepted": True, "ack_id": f"ack_{cid}_v{version}", "stored_at": stored_at}


# ---------------------------------------------------------------------------
# POST /v1/tick
# ---------------------------------------------------------------------------


def _has_consent(customer: dict) -> bool:
    consent = customer.get("consent") or {}
    return bool(consent.get("opted_in_at")) and bool(consent.get("scope"))


def _expired(trg: dict, now: Optional[str]) -> bool:
    exp, cur = parse_dt(trg.get("expires_at")), parse_dt(now)
    return bool(exp and cur and exp < cur)


def _plan_tick(now: Optional[str], trigger_ids: List[str]) -> List[Dict[str, Any]]:
    triggers = []
    seen = set()
    listed = {t for t in trigger_ids if isinstance(t, str)}
    # judge-listed triggers first, then ones we deferred earlier (those must still be unexpired)
    for tid in list(trigger_ids) + list(store.pending):
        if not isinstance(tid, str) or tid in seen:
            continue
        seen.add(tid)
        trg = store.get("trigger", tid)
        if not trg:
            continue
        if tid not in listed and _expired(trg, now):
            store.pending.pop(tid, None)
            continue
        triggers.append((tid, trg))
    triggers.sort(key=lambda x: (-int(x[1].get("urgency") or 0), x[0]))

    actions: List[Dict[str, Any]] = []
    used_audience = set()
    for tid, trg in triggers:
        skey = trg.get("suppression_key") or tid
        if skey in store.sent_suppression:
            store.pending.pop(tid, None)
            continue
        mid = trg.get("merchant_id") or (trg.get("payload") or {}).get("merchant_id")
        merchant = store.get("merchant", mid)
        if mid in store.opted_out:
            store.pending.pop(tid, None)
            continue
        if not merchant:
            store.pending[tid] = None   # merchant context may still arrive
            continue
        category = store.get("category", merchant.get("category_slug"))
        if not category:
            continue
        cid = trg.get("customer_id")
        customer = store.get("customer", cid) if cid else None
        if cid:
            if cid in store.opted_out:
                store.pending.pop(tid, None)
                continue
            if not customer:
                store.pending[tid] = None   # customer context may arrive after the trigger
                continue
            if customer.get("merchant_id") and customer["merchant_id"] != mid:
                continue
            if not _has_consent(customer):
                store.sent_suppression.add(skey)   # no consent on record: never message this customer
                store.pending.pop(tid, None)
                continue
        audience = (mid, cid)
        kind_key = (mid, cid, trg.get("kind"))
        if kind_key in store.sent_kinds:
            store.pending.pop(tid, None)          # same merchant/customer already got this kind of message
            continue
        if audience in used_audience or len(actions) >= MAX_ACTIONS_PER_TICK:
            store.pending[tid] = None             # one message per audience per tick; send next tick
            continue

        msg = compose_full(category, merchant, trg, customer, now)
        conv_id = f"conv_{tid}" if not cid else f"conv_{tid}_{cid}"
        n = 2
        while conv_id in store.conversations:
            conv_id = f"conv_{tid}_{n}"
            n += 1
        role = "customer" if msg["send_as"] == "merchant_on_behalf" else "merchant"
        state = new_state(conv_id, mid, cid, role=role, kind=trg.get("kind"), trigger_id=tid,
                          accept_body=msg["_accept_body"], detail=msg["_detail"], topic=msg["_topic"],
                          hinglish=msg["_hinglish"], merchant_name=(merchant.get("identity") or {}).get("name", ""),
                          slots=[s.get("label") for s in (trg.get("payload") or {}).get("available_slots")
                                 or (trg.get("payload") or {}).get("next_session_options") or [] if isinstance(s, dict)])
        state["sent_bodies"].append(msg["body"])
        state["turns"].append({"from": "bot", "body": msg["body"]})
        store.conversations[conv_id] = state
        store.sent_suppression.add(skey)
        store.sent_kinds.add(kind_key)
        store.pending.pop(tid, None)
        used_audience.add(audience)
        actions.append({
            "conversation_id": conv_id,
            "merchant_id": mid,
            "customer_id": cid,
            "send_as": msg["send_as"],
            "trigger_id": tid,
            "template_name": msg["template_name"],
            "template_params": msg["template_params"],
            "body": msg["body"],
            "cta": msg["cta"],
            "suppression_key": skey,
            "rationale": msg["rationale"],
        })
    return actions


@app.post("/v1/tick")
async def tick(request: Request):
    data, err, _ = await _json_body(request)
    if err:
        return err
    now = data.get("now") if isinstance(data.get("now"), str) and parse_dt(data.get("now")) else None
    trig = data.get("available_triggers") or []
    if not isinstance(trig, list):
        return _bad("invalid_available_triggers", "available_triggers must be a list of trigger ids")
    with store.lock:
        try:
            actions = _plan_tick(now, trig)
        except Exception as e:  # never fail the tick — an empty action list is always valid
            return {"actions": [], "error": f"{type(e).__name__}: {e}"}
    return {"actions": actions}


# ---------------------------------------------------------------------------
# POST /v1/reply
# ---------------------------------------------------------------------------


def _state_for_unknown_conversation(conv_id: str, mid: Optional[str], cid: Optional[str], role: str) -> Dict[str, Any]:
    """Judge may reply on a conversation we never opened (e.g. replay scenarios). Build context from the merchant."""
    merchant = store.get("merchant", mid) or {}
    category = store.get("category", merchant.get("category_slug")) or {}
    kw: Dict[str, Any] = {"merchant_name": (merchant.get("identity") or {}).get("name", ""), "hinglish": False}
    if merchant:
        ctx = Ctx(category, merchant, {"kind": "conversation", "payload": {}}, None, None)
        problem, action, accept = ctx.fix_plan()
        kw.update(accept_body=accept, detail=problem or (ctx.perf_anchor() or ""), topic=action)
    return new_state(conv_id, mid, cid, role=role, **kw)


@app.post("/v1/reply")
async def reply(request: Request):
    data, err, _ = await _json_body(request)
    if err:
        return err
    conv_id = data.get("conversation_id")
    message = data.get("message")
    if not isinstance(conv_id, str) or not conv_id:
        return _bad("invalid_conversation_id", "conversation_id is required")
    if message is not None and not isinstance(message, str):
        return _bad("invalid_message", "message must be a string")
    from_role = data.get("from_role") or "merchant"
    with store.lock:
        state = store.conversations.get(conv_id)
        if state is None:
            role = "customer" if from_role == "customer" else "merchant"
            state = _state_for_unknown_conversation(conv_id, data.get("merchant_id"), data.get("customer_id"), role)
            store.conversations[conv_id] = state
        mid = state.get("merchant_id") or data.get("merchant_id")
        mem = store.merchant_memory.setdefault(mid or "_unknown", {})
        try:
            result = respond(state, message or "", mem)
        except Exception as e:  # a reply must always be well-formed
            result = {"action": "wait", "wait_seconds": 1800, "rationale": f"internal error, backing off ({type(e).__name__})"}
        if state.get("opted_out"):
            who = state.get("customer_id") if state.get("role") == "customer" else mid
            if who:
                store.opted_out.add(who)
    return result


@app.post("/v1/teardown")
def teardown() -> Dict[str, Any]:
    with store.lock:
        store.reset()
    return {"ok": True, "wiped_at": _now_iso()}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
