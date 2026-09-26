"""
Multi-turn reply handling for Vera conversations (deterministic).

respond(state, merchant_message, merchant_memory=None) -> action dict
    {"action": "send", "body", "cta", "rationale"}
    {"action": "wait", "wait_seconds", "rationale"}
    {"action": "end", "rationale"}

`state` is a plain dict (see new_state) that this module mutates in place.
`merchant_memory` is an optional per-merchant dict shared across conversations,
used to spot the same canned auto-reply arriving on different conversation ids.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

MAX_BOT_TURNS = 6

AUTO_REPLY = [
    r"thank(s| you)( so much)? for (contacting|reaching out|reaching|your message|messaging|writing)",
    r"(our|the) (team|executive|representative)s? will (respond|reply|get back|revert|contact|call)",
    r"will (get back|revert|respond) to you (shortly|soon|as soon)",
    r"we('| a)re (currently )?(closed|unavailable|away|offline)",
    r"automated (assistant|message|reply|response)", r"\bauto[- ]?reply\b", r"this is an automated",
    r"outside (our )?(business|working) hours", r"out of (the )?office",
    r"(message|query|request) has been received", r"we have received your (message|query|request)",
    r"aapki jaankari ke liye", r"team tak pahuncha", r"jald(i)? (hi )?(aapse )?sampark",
    r"do not reply to this", r"please (wait|hold) (for|while) (our|an) (agent|executive)",
]
OPT_OUT = [
    r"^\s*stop\s*[.!]*\s*$", r"\bstop (messag|send|text|spamm|bother|contact)", r"not interested", r"\bunsubscribe\b",
    r"(don'?t|do not|never) (message|text|contact|send|call|bother)", r"no more (messages|texts)",
    r"leave me alone", r"band karo", r"mat bhejo", r"message mat", r"nahi chahiye", r"remove me", r"\bopt[- ]?out\b",
    r"interest nahi", r"not (at all )?useful to me",
]
HOSTILE = [
    r"useless", r"\bspam", r"idiot", r"stupid", r"nonsense", r"bakwas", r"pagal", r"shut up", r"\bfraud\b", r"\bscam",
    r"bloody", r"\bdamn", r"f+u+c+k", r"harass", r"bothering", r"irritat", r"waste of (my )?time", r"chutiya", r"bewakoof",
    r"get lost", r"annoying",
]
ACCEPT = [
    r"^(yes|yess+|yeah|yep|yup|haan|haa|han|ha|ji|ji haan|ok|okay|okk+|k|sure|done|confirm(ed)?|go|proceed|chalo|chalega|"
    r"theek hai|thik hai|bilkul|please|pls|absolutely|of course|definitely)\b",
    r"let'?s do (it|this)", r"lets do", r"go ahead", r"\bdo it\b", r"please (do|send|go|proceed|share|draft|start|book)",
    r"\bsend (it|me|the|over)\b", r"sounds good", r"\bkar do\b", r"\bkardo\b", r"kar dijiye", r"kar dena", r"bhej do", r"bhejo",
    r"i want to (join|start|sign|go|do)", r"\bjud(na|a)\b", r"\binterested\b", r"what'?s next", r"whats next", r"next step",
    r"\bbook (it|me|the)\b", r"\bconfirm\b", r"start (it|now)", r"go for it", r"i('| a)m in\b", r"count me in",
]
DECLINE_LATER = [
    r"\blater\b", r"not now", r"\bbusy\b", r"baad mein", r"abhi nahi", r"in a meeting", r"call (me )?later",
    r"\btomorrow\b", r"\bkal\b", r"next week", r"some other time", r"give me (some )?time", r"thodi der",
]
NO = [r"^(no|nope|nah|nahi|nahin|na|not really)\b[\s.!]*$", r"^no,? thanks", r"no thank you", r"not needed", r"don'?t need",
      r"zarurat nahi", r"^(no|nahi),? (i|we|main|hum)"]
THANKS = [r"^(thanks|thank you|thx|ty|shukriya|dhanyavaad|dhanyawad|great|cool|nice|perfect|awesome|super|👍|🙏|ok thanks|thank u)[\s.!🙏👍]*$"]
OFF_TOPIC = [
    (r"\bgst\b", "GST filing"), (r"income tax|\bitr\b|\btax(es)?\b", "tax filing"), (r"\bloan\b", "loans"),
    (r"insurance", "insurance"), (r"\bvisa\b|passport", "visa/passport work"), (r"lawyer|legal notice|court", "legal matters"),
    (r"electricity|water bill|recharge", "bill payments"), (r"aadhaa?r|pan card", "ID documents"),
    (r"bank account|\bupi\b issue", "banking issues"), (r"cricket score|movie", "that"),
]
PRICE = [r"how much", r"\bkitna\b", r"\bkitne\b", r"\bprice\b", r"\bcost", r"\bcharges?\b", r"\bfees?\b", r"\bpaise\b", r"rate kya"]
QUESTION_START = r"^(what|how|why|when|where|which|who|can|could|will|would|is|are|does|do|kya|kaise|kab|kaun|kahan|kitna)\b"
RESCHEDULE = [r"^\s*2\s*$", r"reschedul", r"another (time|day|slot)", r"change (the )?(time|slot|day)", r"can'?t make it", r"nahi aa"]

HINDI_TOKENS = {
    "hai", "haan", "nahi", "nahin", "kya", "kaise", "karo", "kar", "mujhe", "aap", "aapka", "chahiye", "bhej", "theek",
    "accha", "acha", "mein", "hum", "yeh", "woh", "kab", "kitna", "bhai", "sahi", "karna", "hoga", "karein", "dijiye",
    "batao", "bataiye", "chalega", "abhi", "baad", "kal", "ji", "hoon", "hain", "kuch", "matlab", "jaldi",
}


def _any(patterns: List[str], text: str) -> bool:
    return any(re.search(p, text, re.I) for p in patterns)


def detect_hinglish(text: str) -> Optional[bool]:
    """True/False when the turn clearly signals a language, None when ambiguous (e.g. 'yes', 'ok')."""
    if re.search(r"[ऀ-ॿ]", text):
        return True
    words = re.findall(r"[a-z]+", text.lower())
    if len(words) <= 1:
        return None
    hits = sum(1 for w in words if w in HINDI_TOKENS)
    if hits >= 1 and (hits >= 2 or len(words) <= 6):
        return True
    return False


def new_state(conversation_id: str, merchant_id: Optional[str], customer_id: Optional[str] = None,
              role: str = "merchant", **kw: Any) -> Dict[str, Any]:
    st = {
        "conversation_id": conversation_id, "merchant_id": merchant_id, "customer_id": customer_id,
        "role": role, "status": "active", "stage": "pitch", "kind": None, "trigger_id": None,
        "sent_bodies": [], "turns": [], "auto_reply_count": 0, "hostile_count": 0, "offtopic_count": 0,
        "last_inbound": None, "accept_body": "", "detail": "", "topic": "", "hinglish": False,
        "merchant_name": "", "slots": [], "confirm_body": "",
    }
    st.update({k: v for k, v in kw.items() if v is not None})
    return st


def _send(state: Dict[str, Any], body: str, cta: str, rationale: str) -> Dict[str, Any]:
    body = _unique(state, body.strip())
    state["sent_bodies"].append(body)
    state["turns"].append({"from": "bot", "body": body})
    return {"action": "send", "body": body, "cta": cta, "rationale": rationale}


def _end(state: Dict[str, Any], rationale: str) -> Dict[str, Any]:
    state["status"] = "ended"
    return {"action": "end", "rationale": rationale}


def _wait(state: Dict[str, Any], seconds: int, rationale: str) -> Dict[str, Any]:
    state["status"] = "waiting"
    return {"action": "wait", "wait_seconds": int(seconds), "rationale": rationale}


VARIANT_TAILS = [" (Just a YES is enough.)", " — whenever you're ready.", " 🙂", " (No rush.)"]


def _unique(state: Dict[str, Any], body: str) -> str:
    """Never send the exact same body twice in one conversation."""
    if body not in state["sent_bodies"]:
        return body
    for tail in VARIANT_TAILS:
        cand = body.rstrip() + tail
        if cand not in state["sent_bodies"]:
            return cand
    return body + f" [{len(state['sent_bodies']) + 1}]"


def _t(state: Dict[str, Any], en: str, hi: str) -> str:
    return hi if state.get("hinglish") else en


def _topic(state: Dict[str, Any]) -> str:
    return state.get("topic") or "your Google listing"


def _default_confirm(state: Dict[str, Any]) -> str:
    return _t(state,
              "Done ✅ It's queued and going live. I'll report back here with the results in 7 days.",
              "Done ✅ Queue ho gaya, live ja raha hai. 7 din mein results yahin share karungi.")


# ---------------------------------------------------------------------------


def respond(state: Dict[str, Any], merchant_message: str,
            merchant_memory: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    msg = (merchant_message or "").strip()
    low = re.sub(r"\s+", " ", msg.lower())
    state.setdefault("turns", []).append({"from": state.get("role", "merchant"), "body": msg})

    if state.get("status") == "ended":
        return {"action": "end", "rationale": "Conversation already closed; not sending further messages."}
    state["status"] = "active"

    lang = detect_hinglish(msg)
    if lang is not None:
        state["hinglish"] = lang

    if not msg:
        return _wait(state, 1800, "Empty inbound message; waiting for a real reply.")

    bot_turns = sum(1 for t in state["turns"] if t.get("from") == "bot")

    # 1) explicit opt-out ---------------------------------------------------
    if _any(OPT_OUT, low):
        state["opted_out"] = True
        return _end(state, "Explicit opt-out / not interested — closing and suppressing further sends to this contact.")

    # 2) auto-reply detection (pattern OR same text repeated, tracked per conversation and per merchant)
    mem = merchant_memory if merchant_memory is not None else {}
    # verbatim repeats only count for longer messages — a merchant can legitimately say "yes" twice
    long_enough = len(low) >= 25
    repeated = long_enough and (low == (state.get("last_inbound") or "")
                                or (low == mem.get("last_inbound") and mem.get("auto_count", 0) > 0))
    if _any(AUTO_REPLY, low) or repeated:
        state["auto_reply_count"] = state.get("auto_reply_count", 0) + 1
        if (long_enough and mem.get("last_inbound") == low) or _any(AUTO_REPLY, low):
            mem["auto_count"] = mem.get("auto_count", 0) + 1
        mem["last_inbound"] = low
        state["last_inbound"] = low
        n = max(state["auto_reply_count"], mem.get("auto_count", 0))
        if n == 1:
            body = _t(state,
                      f"Looks like an auto-reply — no problem. When the owner sees this: a one-word YES and I'll take care of {_topic(state)}.",
                      f"Lagta hai yeh auto-reply hai 🙂 Owner dekhein toh bas YES bhej dein — {_topic(state)} main sambhal lungi.")
            return _send(state, body, "binary_yes_no",
                         "Detected WhatsApp Business auto-reply (canned phrasing); one short owner-directed nudge, no repeat pitch.")
        if n == 2:
            return _wait(state, 86400, "Same auto-reply again — owner not at the phone. Backing off 24h instead of burning turns.")
        return _end(state, f"Auto-reply {n}x with no human response — closing conversation to avoid spamming.")
    state["last_inbound"] = low
    mem["last_inbound"] = low
    mem["auto_count"] = 0
    state["auto_reply_count"] = 0

    if bot_turns >= MAX_BOT_TURNS:
        return _end(state, f"Reached {MAX_BOT_TURNS} bot turns in this conversation — exiting gracefully rather than over-messaging.")

    if state.get("role") == "customer":
        return _respond_customer(state, low)

    # 3) hostility without an explicit opt-out ------------------------------
    if _any(HOSTILE, low):
        state["hostile_count"] = state.get("hostile_count", 0) + 1
        if state["hostile_count"] >= 2:
            return _end(state, "Repeated frustration from merchant — closing politely without further pitching.")
        body = _t(state,
                  f"Sorry for the bother — I won't push. I only message when there's something specific for {state.get('merchant_name') or 'your business'}. "
                  "Reply STOP and I won't message again.",
                  f"Sorry for the bother — main push nahi karungi. Sirf tab message karti hoon jab {state.get('merchant_name') or 'aapke business'} ke liye kuch specific ho. "
                  "STOP reply karein toh main dobara message nahi karungi.")
        return _send(state, body, "none", "Merchant frustrated; short apology + explicit opt-out path, no pitch.")

    # 4) off-topic request --------------------------------------------------
    for pat, label in OFF_TOPIC:
        if re.search(pat, low, re.I):
            state["offtopic_count"] = state.get("offtopic_count", 0) + 1
            if label == "that":
                decline = _t(state, "That's outside what I can help with.", "Woh mere scope ke bahar hai.")
            else:
                decline = _t(state, f"{label[:1].upper() + label[1:]} is outside what I can help with — your CA / the right specialist is best for that.",
                             f"{label[:1].upper() + label[1:]} mere scope ke bahar hai — iske liye aapke CA / specialist best rahenge.")
            back = _t(state, f"What I can do right now is {_topic(state)} — reply YES and I'll get it moving.",
                      f"Main abhi {_topic(state)} kar sakti hoon — YES reply karein, main shuru kar deti hoon.")
            return _send(state, f"{decline} {back}", "binary_yes_no",
                         f"Out-of-scope ask ({label}) declined politely; redirected to the original thread.")

    # 5) plain no -------------------------------------------------------------
    if _any(NO, low):
        return _end(state, "Merchant declined; exiting gracefully instead of re-pitching.")

    # 6) not now ---------------------------------------------------------------
    if _any(DECLINE_LATER, low) and not _any(ACCEPT[1:], low):
        long = re.search(r"tomorrow|\bkal\b|next week", low)
        secs = 86400 if long else (1800 if re.search(r"meeting|30 ?min|thodi der", low) else 3600)
        return _wait(state, secs, f"Merchant asked for time; backing off {secs // 60} min before following up.")

    # 7) price question -------------------------------------------------------
    if _any(PRICE, low):
        prices = re.findall(r"₹[\d,]+", state.get("accept_body", "") + " " + state.get("detail", ""))
        if prices:
            body = _t(state, f"The figure on the table is {prices[0]} — that's straight from your current plan/offer, nothing hidden. "
                             f"Want me to go ahead with {_topic(state)}? Reply YES.",
                      f"Figure {prices[0]} hai — seedha aapke current plan/offer se, kuch hidden nahi. {_topic(state)} ke saath aage badhun? Reply YES.")
        else:
            body = _t(state, "Nothing gets charged or published without your OK — I'll show you the exact figure before anything goes live. "
                             f"Shall I start on {_topic(state)}? Reply YES.",
                      "Aapke OK ke bina kuch charge ya publish nahi hoga — live karne se pehle exact figure dikhaungi. "
                      f"{_topic(state)} shuru karun? Reply YES.")
        return _send(state, body, "binary_yes_no", "Price question answered only with figures present in context; no invented pricing.")

    # 8) acceptance / intent → action mode ----------------------------------
    if _any(ACCEPT, low):
        return _advance(state, "Merchant committed — switching straight to action (no further qualifying questions).")

    # 9) thanks ------------------------------------------------------------
    if _any(THANKS, low):
        if state.get("stage") in ("confirmed", "action_offered"):
            return _end(state, "Merchant acknowledged; work is delivered — closing warmly without another ask.")
        body = _t(state, f"Glad it's useful! If you want me to handle {_topic(state)}, just reply YES.",
                  f"Khushi hui! {_topic(state)} main handle kar doon toh bas YES reply karein.")
        return _send(state, body, "binary_yes_no", "Polite acknowledgement; single low-friction CTA, no re-pitch.")

    # 10) a question → answer from stored detail ---------------------------
    if "?" in msg or re.search(QUESTION_START, low):
        detail = (state.get("detail") or "").strip()
        if detail:
            body = _t(state, f"Good question. In short: {detail[:420].rstrip()} Want me to go ahead with {_topic(state)}? Reply YES.",
                      f"Accha sawaal. Short mein: {detail[:420].rstrip()} {_topic(state)} ke saath aage badhun? Reply YES.")
        else:
            body = _t(state, f"I'll check that and come back to you here. Meanwhile, want me to start on {_topic(state)}? Reply YES.",
                      f"Main check karke yahin batati hoon. Tab tak {_topic(state)} shuru kar doon? Reply YES.")
        return _send(state, body, "binary_yes_no", "Merchant asked a question; answered from context facts only, then one CTA.")

    # 11) any other substantive message = engagement → move to action ---------
    if state.get("kind") in ("curious_ask_due", "scheduled_recurring") and state.get("stage") == "pitch":
        answer = msg[:80].strip().rstrip(".")
        state["stage"] = "action_offered"
        body = _t(state,
                  f"Got it — {answer}. Here's the Google post draft:\n\"{answer} at {state.get('merchant_name') or 'our place'} — ask us about it today!\"\n"
                  "And a ready WhatsApp reply for price questions. Reply CONFIRM to publish the post.",
                  f"Samajh gayi — {answer}. Google post draft:\n\"{answer} at {state.get('merchant_name') or 'our place'} — aaj hi poochiye!\"\n"
                  "Price questions ke liye WhatsApp reply bhi ready hai. Post publish karne ke liye CONFIRM reply karein.")
        return _send(state, body, "binary_confirm_cancel", "Merchant answered the curious-ask; turned their answer into a ready draft immediately.")
    return _advance(state, "Engaged reply — moving to the concrete next step rather than asking more questions.")


def _hinglishify(state: Dict[str, Any], body: str) -> str:
    """Drafts stay as written (they are customer/Google-facing); Vera's own framing follows the merchant's language."""
    if not state.get("hinglish") or not body:
        return body
    body = re.sub(r"Reply CONFIRM", "Check karke CONFIRM reply karein —", body, count=1)
    return "Bilkul, abhi kar deti hoon. " + body


def _advance(state: Dict[str, Any], rationale: str) -> Dict[str, Any]:
    stage = state.get("stage", "pitch")
    if stage == "pitch":
        state["stage"] = "action_offered"
        body = _hinglishify(state, state.get("accept_body")) or _t(
            state,
            "Great — starting now. I'm preparing the draft and you'll see the preview here within 10 minutes. Reply CONFIRM to publish once you've checked it.",
            "Great — abhi shuru kar rahi hoon. Draft ka preview 10 min mein yahin milega. Check karke CONFIRM reply karein.")
        return _send(state, body, "binary_confirm_cancel", rationale)
    if stage == "action_offered":
        state["stage"] = "confirmed"
        return _send(state, state.get("confirm_body") or _default_confirm(state), "none",
                     "Merchant confirmed — executing and closing the loop with a concrete follow-up promise.")
    return _end(state, "Action already confirmed and delivered; nothing further to ask — closing gracefully.")


def _respond_customer(state: Dict[str, Any], low: str) -> Dict[str, Any]:
    slots = state.get("slots") or []
    name = state.get("merchant_name") or "us"
    if state.get("kind") == "appointment_tomorrow" and _any(RESCHEDULE, low):
        return _send(state, _t(state, "No problem — tell us a day and time that suits you and we'll move it.",
                               "Koi baat nahi — jo din aur time suit kare bata dijiye, hum shift kar denge."),
                     "open_ended", "Customer wants to reschedule; asking for their preferred time.")
    if _any(NO, low):
        return _end(state, "Customer declined; closing politely.")
    if _any(DECLINE_LATER, low):
        return _wait(state, 86400, "Customer asked for later; following up tomorrow.")
    pick = re.match(r"^\s*([1-9])\b", low)
    if pick and slots:
        idx = min(int(pick.group(1)) - 1, len(slots) - 1)
        if state.get("stage") == "confirmed":
            return _end(state, "Booking already confirmed.")
        state["stage"] = "confirmed"
        return _send(state, _t(state, f"Booked ✅ {slots[idx]} is confirmed at {name}. We'll remind you the day before.",
                               f"Booked ✅ {slots[idx]} {name} mein confirm hai. Ek din pehle reminder bhej denge."),
                     "none", f"Customer picked slot {idx + 1}; confirming booking.")
    if _any(ACCEPT, low) or pick:
        if state.get("stage") == "confirmed":
            return _end(state, "Booking/request already confirmed; nothing more to send.")
        state["stage"] = "confirmed"
        return _send(state, state.get("accept_body") or _t(state, "Done ✅ We'll confirm the details here shortly.",
                                                           "Done ✅ Details yahin confirm kar denge."),
                     "none", "Customer accepted; confirming on the merchant's behalf.")
    if _any(THANKS, low):
        return _end(state, "Customer acknowledged; closing.")
    return _send(state, _t(state, f"Thanks for your message — the {name} team will get back to you on this shortly.",
                           f"Message ke liye shukriya — {name} team jaldi aapko reply karegi."),
                 "none", "Customer asked something outside the templated flow; routed to merchant's team without guessing.")
