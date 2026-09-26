import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bot  # noqa: E402
from composer import compose  # noqa: E402

DATASET = ROOT / "dataset"
EXPANDED = DATASET / "expanded"
REQUIRED_ACTION_KEYS = {"conversation_id", "merchant_id", "customer_id", "send_as", "trigger_id", "template_name",
                        "template_params", "body", "cta", "suppression_key", "rationale"}
QUALIFYING = ["would you", "do you", "can you tell", "what if", "how about"]
ACTIONING = ["done", "sending", "draft", "here", "confirm", "proceed", "next"]


@pytest.fixture(scope="session")
def data():
    if not (EXPANDED / "test_pairs.json").exists():
        subprocess.run([sys.executable, "generate_dataset.py", "--out", "./expanded"], cwd=DATASET, check=True)

    def load(sub, key):
        out = {}
        for f in sorted((EXPANDED / sub).glob("*.json")):
            d = json.loads(f.read_text())
            out[d[key]] = d
        return out

    return {
        "categories": load("categories", "slug"),
        "merchants": load("merchants", "merchant_id"),
        "customers": load("customers", "customer_id"),
        "triggers": load("triggers", "id"),
        "pairs": json.loads((EXPANDED / "test_pairs.json").read_text())["pairs"],
    }


@pytest.fixture()
def client():
    bot.store.reset()
    return TestClient(bot.app)


def push(client, scope, cid, payload, version=1):
    return client.post("/v1/context", json={"scope": scope, "context_id": cid, "version": version,
                                            "payload": payload, "delivered_at": "2026-04-26T10:00:00Z"})


def warmup(client, data, triggers=True):
    for slug, c in data["categories"].items():
        assert push(client, "category", slug, c).status_code == 200
    for mid, m in data["merchants"].items():
        assert push(client, "merchant", mid, m).status_code == 200
    for cid, c in data["customers"].items():
        assert push(client, "customer", cid, c).status_code == 200
    if triggers:
        for tid, t in data["triggers"].items():
            assert push(client, "trigger", tid, t).status_code == 200


def taboo_words(data):
    out = set()
    for c in data["categories"].values():
        for t in c.get("voice", {}).get("vocab_taboo", []):
            out.add(re.sub(r"\s*\(.*?\)", "", t).strip().lower())
    return out


# --- health / metadata -------------------------------------------------------

def test_healthz_and_metadata(client):
    r = client.get("/v1/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["contexts_loaded"] == {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    m = client.get("/v1/metadata").json()
    for k in ("team_name", "team_members", "model", "approach", "contact_email", "version", "submitted_at"):
        assert k in m


# --- context -----------------------------------------------------------------

def test_context_versioning_and_validation(client, data):
    m = data["merchants"]["m_001_drmeera_dentist_delhi"]
    r = push(client, "merchant", m["merchant_id"], m)
    assert r.status_code == 200 and r.json()["accepted"] is True and "ack_id" in r.json() and "stored_at" in r.json()
    r = push(client, "merchant", m["merchant_id"], m)          # same version -> stale
    assert r.status_code == 409 and r.json() == {"accepted": False, "reason": "stale_version", "current_version": 1}
    assert push(client, "merchant", m["merchant_id"], m, version=2).status_code == 200
    assert push(client, "merchant", m["merchant_id"], m, version=1).json()["current_version"] == 2
    assert push(client, "nonsense", "x", {}).json()["reason"] == "invalid_scope"
    assert push(client, "merchant", "x", "not-a-dict").status_code == 400
    assert client.post("/v1/context", json={"scope": "merchant", "context_id": "x", "version": "1", "payload": {}}).status_code == 400
    assert client.post("/v1/context", content=b"{bad json", headers={"content-type": "application/json"}).status_code == 400
    assert client.get("/v1/healthz").json()["contexts_loaded"]["merchant"] == 1


def test_warmup_counts(client, data):
    warmup(client, data, triggers=False)
    assert client.get("/v1/healthz").json()["contexts_loaded"] == {"category": 5, "merchant": 50, "customer": 200, "trigger": 0}


# --- tick --------------------------------------------------------------------

def test_tick_all_triggers(client, data):
    warmup(client, data)
    taboos = taboo_words(data)
    tids = list(data["triggers"])
    all_actions = []
    for minute in range(0, 60, 5):   # judge ticks repeatedly; bot sends <=1 message per audience per tick
        r = client.post("/v1/tick", json={"now": f"2026-04-26T10:{minute:02d}:00Z", "available_triggers": tids})
        assert r.status_code == 200
        acts = r.json()["actions"]
        assert len(acts) <= 20
        audiences = [(a["merchant_id"], a["customer_id"]) for a in acts]
        assert len(audiences) == len(set(audiences))
        all_actions += acts
        if not acts:
            break
    assert len(all_actions) >= 85
    convs = set()
    for a in all_actions:
        assert REQUIRED_ACTION_KEYS <= set(a), a
        assert a["body"].strip() and a["rationale"].strip()
        assert not re.search(r"https?://|www\.", a["body"])
        low = a["body"].lower()
        assert not any(t and t in low for t in taboos), (a["trigger_id"], a["body"])
        trg = data["triggers"][a["trigger_id"]]
        assert a["merchant_id"] == trg["merchant_id"]
        if trg["scope"] == "customer":
            assert a["send_as"] == "merchant_on_behalf" and a["customer_id"] == trg["customer_id"]
        else:
            assert a["send_as"] == "vera"
        assert a["conversation_id"] not in convs
        convs.add(a["conversation_id"])
    # suppression: re-ticking the same triggers sends nothing new
    r = client.post("/v1/tick", json={"now": "2026-04-26T10:35:00Z", "available_triggers": tids})
    assert r.json()["actions"] == []


def test_tick_edge_cases(client, data):
    assert client.post("/v1/tick", json={"now": "2026-04-26T10:30:00Z", "available_triggers": []}).json() == {"actions": []}
    assert client.post("/v1/tick", json={"now": "2026-04-26T10:30:00Z", "available_triggers": ["missing"]}).json() == {"actions": []}
    assert client.post("/v1/tick", json={"available_triggers": "x"}).status_code == 400
    # trigger present but merchant not yet pushed -> nothing
    t = data["triggers"]["trg_001_research_digest_dentists"]
    push(client, "trigger", t["id"], t)
    assert client.post("/v1/tick", json={"now": "2026-04-26T10:30:00Z", "available_triggers": [t["id"]]}).json() == {"actions": []}


def test_customer_without_consent_is_skipped(client, data):
    warmup(client, data, triggers=False)
    trg = {"id": "trg_x", "scope": "customer", "kind": "recall_due", "source": "internal",
           "merchant_id": "m_010_sunrisepharm_pharmacy_lucknow", "customer_id": "c_015_anonymous_for_m010",
           "payload": {}, "urgency": 3, "suppression_key": "x", "expires_at": "2026-12-01T00:00:00Z"}
    push(client, "trigger", "trg_x", trg)
    assert client.post("/v1/tick", json={"now": "2026-04-26T10:30:00Z", "available_triggers": ["trg_x"]}).json()["actions"] == []


def _start(client, data, tid):
    warmup(client, data)
    acts = client.post("/v1/tick", json={"now": "2026-04-26T10:30:00Z", "available_triggers": [tid]}).json()["actions"]
    assert len(acts) == 1
    return acts[0]


def reply(client, conv, msg, turn=2, mid="m_001_drmeera_dentist_delhi", role="merchant", cid=None):
    return client.post("/v1/reply", json={"conversation_id": conv, "merchant_id": mid, "customer_id": cid,
                                          "from_role": role, "message": msg,
                                          "received_at": "2026-04-26T10:45:00Z", "turn_number": turn}).json()


# --- reply -------------------------------------------------------------------

def test_engaged_reply_goes_to_action(client, data):
    a = _start(client, data, "trg_001_research_digest_dentists")
    r = reply(client, a["conversation_id"], "Yes please send the abstract. Also draft the patient WhatsApp.")
    assert r["action"] == "send" and r["body"] and r["body"] != a["body"]
    r2 = reply(client, a["conversation_id"], "Confirm", 3)
    assert r2["action"] == "send"
    r3 = reply(client, a["conversation_id"], "thanks", 4)
    assert r3["action"] == "end"


def test_auto_reply_same_conversation(client, data):
    a = _start(client, data, "trg_022_cde_webinar_dentists")
    canned = "Thank you for contacting Dr. Meera's Dental Clinic! Our team will respond shortly."
    actions = [reply(client, a["conversation_id"], canned, t)["action"] for t in (2, 3, 4)]
    assert actions == ["send", "wait", "end"]
    assert reply(client, a["conversation_id"], canned, 5)["action"] == "end"


def test_auto_reply_across_conversations_like_simulator(client, data):
    warmup(client, data, triggers=False)
    msg = "Thank you for contacting us! Our team will respond shortly."
    got = [reply(client, f"conv_auto_{i}", msg, i + 1)["action"] for i in range(1, 5)]
    assert "end" in got[:3]


def test_intent_transition(client, data):
    warmup(client, data, triggers=False)
    r = reply(client, "conv_intent_1", "Ok lets do it. Whats next?")
    body = r["body"].lower()
    assert r["action"] == "send"
    assert any(w in body for w in ACTIONING) and not any(w in body for w in QUALIFYING)
    r = reply(client, "conv_join", "Mujhe magicpin judna hai")
    assert r["action"] == "send" and not any(w in r["body"].lower() for w in QUALIFYING)


def test_hostile_and_opt_out(client, data):
    warmup(client, data, triggers=False)
    assert reply(client, "conv_hostile", "Stop messaging me. This is useless spam.")["action"] == "end"
    assert reply(client, "conv_hostile", "hello?")["action"] == "end"
    r = reply(client, "conv_h2", "You people are useless")
    assert r["action"] == "send" and ("sorry" in r["body"].lower() or "won't" in r["body"].lower())
    r = reply(client, "conv_h2", "can you also help me file my GST?")
    assert r["action"] == "send" and "gst" in r["body"].lower()
    assert reply(client, "conv_h3", "Not interested.")["action"] == "end"


def test_opt_out_suppresses_future_ticks(client, data):
    a = _start(client, data, "trg_001_research_digest_dentists")
    assert reply(client, a["conversation_id"], "Not interested. Stop messaging me.")["action"] == "end"
    r = client.post("/v1/tick", json={"now": "2026-04-26T11:00:00Z", "available_triggers": ["trg_023_competitor_opened_dentist"]})
    assert r.json()["actions"] == []


def test_wait_no_and_offtopic(client, data):
    a = _start(client, data, "trg_004_perf_dip_bharat")
    cid = a["conversation_id"]
    r = reply(client, cid, "Busy right now, in a meeting")
    assert r["action"] == "wait" and r["wait_seconds"] > 0
    r = reply(client, cid, "Btw can you also help me with my GST filing this month?", 3)
    assert r["action"] == "send" and "gst" in r["body"].lower()
    assert reply(client, cid, "no", 4)["action"] == "end"


def test_no_repeated_bodies(client, data):
    a = _start(client, data, "trg_002_compliance_dci_radiograph")
    bodies = [a["body"]]
    for t in range(2, 6):
        r = reply(client, a["conversation_id"], "What exactly changes for me?", t)
        if r["action"] != "send":
            break
        bodies.append(r["body"])
    assert len(bodies) == len(set(bodies))


def test_hindi_turn_detection(client, data):
    a = _start(client, data, "trg_023_competitor_opened_dentist")
    r = reply(client, a["conversation_id"], "haan theek hai, kya karna hoga? kab tak ho jayega?")
    assert r["action"] == "send"
    assert re.search(r"\b(hai|kar|main|aap|yahin|karein|mein)\b", r["body"].lower())


def test_customer_slot_booking(client, data):
    a = _start(client, data, "trg_003_recall_due_priya")
    assert a["send_as"] == "merchant_on_behalf"
    r = reply(client, a["conversation_id"], "2", role="customer", cid="c_001_priya_for_m001")
    assert r["action"] == "send" and "Thu 6 Nov, 5pm" in r["body"]


# --- composition ---------------------------------------------------------------

def test_compose_test_pairs_deterministic(data):
    for p in data["pairs"]:
        trg = data["triggers"][p["trigger_id"]]
        m = data["merchants"][p["merchant_id"]]
        cat = data["categories"][m["category_slug"]]
        cust = data["customers"].get(p["customer_id"]) if p["customer_id"] else None
        a = compose(cat, m, trg, cust)
        b = compose(cat, m, trg, cust)
        assert a == b
        for k in ("body", "cta", "send_as", "suppression_key", "rationale"):
            assert a[k] not in (None, ""), (p, k)
        assert a["send_as"] == ("merchant_on_behalf" if cust else "vera")


def test_new_context_version_is_used(client, data):
    warmup(client, data)
    m = json.loads(json.dumps(data["merchants"]["m_002_bharat_dentist_mumbai"]))
    m["performance"]["views"] = 4321
    assert push(client, "merchant", m["merchant_id"], m, version=2).status_code == 200
    acts = client.post("/v1/tick", json={"now": "2026-04-26T10:30:00Z", "available_triggers": ["trg_004_perf_dip_bharat"]}).json()["actions"]
    assert "4,321" in acts[0]["body"]


def test_teardown(client, data):
    warmup(client, data, triggers=False)
    assert client.post("/v1/teardown").json()["ok"] is True
    assert client.get("/v1/healthz").json()["contexts_loaded"]["merchant"] == 0


def test_deferred_triggers_sent_next_tick(client, data):
    warmup(client, data)
    # two triggers for the same merchant listed once each (simulator style) -> both eventually sent
    ids = ["trg_001_research_digest_dentists", "trg_002_compliance_dci_radiograph"]
    first = client.post("/v1/tick", json={"now": "2026-04-26T10:30:00Z", "available_triggers": ids}).json()["actions"]
    second = client.post("/v1/tick", json={"now": "2026-04-26T10:35:00Z", "available_triggers": []}).json()["actions"]
    assert len(first) == 1 and len(second) == 1
    assert {first[0]["trigger_id"], second[0]["trigger_id"]} == set(ids)


def test_customer_context_arriving_after_trigger(client, data):
    for slug, c in data["categories"].items():
        push(client, "category", slug, c)
    m = data["merchants"]["m_001_drmeera_dentist_delhi"]
    push(client, "merchant", m["merchant_id"], m)
    t = data["triggers"]["trg_003_recall_due_priya"]
    push(client, "trigger", t["id"], t)
    assert client.post("/v1/tick", json={"now": "2026-04-26T10:30:00Z", "available_triggers": [t["id"]]}).json()["actions"] == []
    push(client, "customer", "c_001_priya_for_m001", data["customers"]["c_001_priya_for_m001"])
    acts = client.post("/v1/tick", json={"now": "2026-04-26T10:35:00Z", "available_triggers": []}).json()["actions"]
    assert len(acts) == 1 and acts[0]["send_as"] == "merchant_on_behalf"
