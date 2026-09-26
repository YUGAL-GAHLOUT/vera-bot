"""
Deterministic message composer for the magicpin Vera challenge.

compose(category, merchant, trigger, customer=None, now=None) -> dict

Every message is built only from facts present in the four contexts
(category / merchant / trigger / customer). Nothing is invented: when a
trigger payload is a placeholder, the composer falls back to the merchant's
own numbers and the category's peer stats, digest, seasonal beats and
catalog — never to made-up names, figures or citations.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

# Simulated "today" used when the caller does not pass `now` (offline
# composition for submission.jsonl). Matches the dataset's reference week.
REFERENCE_NOW = "2026-04-26T10:00:00Z"

# ---------------------------------------------------------------------------
# Small formatting helpers
# ---------------------------------------------------------------------------


def g(d: Any, *keys: Any, default: Any = None) -> Any:
    """Safe nested get."""
    cur = d
    for k in keys:
        if isinstance(cur, dict):
            cur = cur.get(k)
        elif isinstance(cur, list) and isinstance(k, int) and -len(cur) <= k < len(cur):
            cur = cur[k]
        else:
            return default
        if cur is None:
            return default
    return cur


def fmt_int(n: Any) -> str:
    """Indian digit grouping: 124000 -> 1,24,000."""
    try:
        n = int(round(float(n)))
    except (TypeError, ValueError):
        return str(n)
    s = str(abs(n))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts: List[str] = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts + [tail])
    return ("-" if n < 0 else "") + s


def rupees(n: Any) -> str:
    return "₹" + fmt_int(n)


def pct(x: Any, signed: bool = False) -> str:
    try:
        v = float(x) * 100
    except (TypeError, ValueError):
        return str(x)
    txt = f"{abs(v):.0f}%"
    if signed:
        return ("+" if v >= 0 else "-") + txt
    return txt


def is_are(label: str) -> str:
    return "is" if label.endswith("rate") else "are"


def pct1(x: Any) -> str:
    try:
        return f"{float(x) * 100:.1f}%"
    except (TypeError, ValueError):
        return str(x)


def parse_dt(s: Any) -> Optional[datetime]:
    if not s or not isinstance(s, str):
        return None
    txt = s.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        try:
            dt = datetime.strptime(txt[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def fmt_date(dt: Optional[datetime], weekday: bool = False) -> str:
    if not dt:
        return ""
    base = f"{dt.day} {dt.strftime('%b')}"
    return f"{dt.strftime('%a')} {base}" if weekday else base


def fmt_time(dt: Optional[datetime]) -> str:
    if not dt:
        return ""
    h = dt.hour % 12 or 12
    mins = f":{dt.minute:02d}" if dt.minute else ""
    return f"{h}{mins}{'am' if dt.hour < 12 else 'pm'}"


def days_between(a: Optional[datetime], b: Optional[datetime]) -> Optional[int]:
    if not a or not b:
        return None
    return (b.date() - a.date()).days


def humanize(s: Any) -> str:
    return str(s).replace("_", " ").strip()


def first_sentence(text: str) -> str:
    text = (text or "").strip()
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    return m.group(1) if m else text


def join_list(items: List[str]) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


URL_RE = re.compile(r"https?://\S+|www\.\S+", re.I)


# ---------------------------------------------------------------------------
# Category vocabulary
# ---------------------------------------------------------------------------

CATEGORY_WORDS = {
    "dentists": {"biz": "clinic", "people": "patients", "peers": "dental clinics", "emoji": "🦷"},
    "salons": {"biz": "salon", "people": "clients", "peers": "salons", "emoji": "✨"},
    "restaurants": {"biz": "restaurant", "people": "customers", "peers": "restaurants", "emoji": "🍽️"},
    "gyms": {"biz": "gym", "people": "members", "peers": "gyms", "emoji": "💪"},
    "pharmacies": {"biz": "pharmacy", "people": "customers", "peers": "pharmacies", "emoji": "💊"},
}

METRIC_LABEL = {
    "views": "profile views",
    "calls": "calls",
    "ctr": "click-through rate",
    "directions": "direction requests",
    "leads": "leads",
    "review_count": "Google reviews",
    "reviews": "Google reviews",
}

MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def _month_range_contains(month_range: str, month_idx: int) -> bool:
    """'Nov-Feb' / 'Apr-Jun' / 'Jan' / 'Feb 14' contain month_idx (0-based)?"""
    found = [MONTHS.index(t[:3].lower()) for t in re.findall(r"[A-Za-z]{3,}", month_range or "")
             if t[:3].lower() in MONTHS]
    if not found:
        return False
    if len(found) == 1:
        return found[0] == month_idx
    start, end = found[0], found[-1]
    if start <= end:
        return start <= month_idx <= end
    return month_idx >= start or month_idx <= end


# ---------------------------------------------------------------------------
# Composition context
# ---------------------------------------------------------------------------


@dataclass
class Draft:
    core: List[str]
    cta_line: str
    cta: str = "binary_yes_no"
    rationale: str = ""
    accept_body: str = ""
    detail: str = ""
    topic: str = ""
    opening: Optional[str] = None  # overrides salutation opener
    facts: Dict[str, Any] = field(default_factory=dict)


class Ctx:
    def __init__(self, category: dict, merchant: dict, trigger: dict,
                 customer: Optional[dict], now: Optional[str]):
        self.cat = category or {}
        self.m = merchant or {}
        self.t = trigger or {}
        self.c = customer or None
        self.p = self.t.get("payload") or {}
        self.now = parse_dt(now) or parse_dt(REFERENCE_NOW)
        self.slug = self.cat.get("slug") or self.m.get("category_slug") or "business"
        self.words = CATEGORY_WORDS.get(self.slug, {
            "biz": "business", "people": "customers",
            "peers": (self.cat.get("display_name") or "businesses").lower(), "emoji": ""})
        ident = self.m.get("identity") or {}
        self.name = ident.get("name") or "your business"
        self.owner = (ident.get("owner_first_name") or "").strip()
        self.city = ident.get("city") or ""
        self.locality = ident.get("locality") or ""
        langs = [str(x).lower() for x in (ident.get("languages") or [])]
        self.hinglish = "hi" in langs
        self.perf = self.m.get("performance") or {}
        self.peer = self.cat.get("peer_stats") or {}
        self.agg = self.m.get("customer_aggregate") or {}
        self.signals = [str(s) for s in (self.m.get("signals") or [])]
        self.placeholder = bool(self.p.get("placeholder"))

    # --- merchant helpers -------------------------------------------------
    @property
    def sal(self) -> str:
        if self.slug == "dentists":
            if self.owner.lower().startswith("dr"):
                return self.owner
            return f"Dr. {self.owner}" if self.owner else self.name
        return self.owner or self.name

    def hi(self, en: str, hing: str) -> str:
        return hing if self.hinglish else en

    def where(self) -> str:
        return ", ".join(x for x in [self.locality, self.city] if x)

    def active_offers(self) -> List[str]:
        return [o.get("title") for o in (self.m.get("offers") or [])
                if o.get("status") == "active" and o.get("title")]

    def expired_offers(self) -> List[str]:
        return [o.get("title") for o in (self.m.get("offers") or [])
                if o.get("status") in ("expired", "paused") and o.get("title")]

    def catalog_offer(self) -> Optional[str]:
        for o in self.cat.get("offer_catalog") or []:
            if o.get("type") in ("service_at_price", "free_service", "free_trial") and o.get("title"):
                return o["title"]
        cat = self.cat.get("offer_catalog") or []
        return cat[0].get("title") if cat else None

    def lead_offer(self) -> Tuple[Optional[str], bool]:
        """(offer title, is_live_on_merchant)."""
        live = self.active_offers()
        if live:
            return live[0], True
        return self.catalog_offer(), False

    def signal_value(self, prefix: str) -> Optional[str]:
        for s in self.signals:
            if s.startswith(prefix):
                rest = s[len(prefix):].lstrip(":_")
                return rest or s
        return None

    def cohort_line(self) -> Optional[str]:
        """Merchant-specific audience anchor from customer_aggregate."""
        a = self.agg
        if a.get("high_risk_adult_count"):
            return f"your {fmt_int(a['high_risk_adult_count'])} high-risk adult patients"
        if a.get("chronic_rx_count"):
            return f"your {fmt_int(a['chronic_rx_count'])} chronic-Rx customers"
        if a.get("total_active_members"):
            return f"your {fmt_int(a['total_active_members'])} active members"
        if a.get("lapsed_180d_plus"):
            return f"the {fmt_int(a['lapsed_180d_plus'])} {self.words['people']} lapsed 180+ days"
        if a.get("lapsed_90d_plus"):
            return f"the {fmt_int(a['lapsed_90d_plus'])} {self.words['people']} lapsed 90+ days"
        if a.get("total_unique_ytd"):
            return f"your {fmt_int(a['total_unique_ytd'])} {self.words['people']} this year"
        return None

    def audience_count(self) -> Optional[str]:
        a = self.agg
        for k in ("chronic_rx_count", "total_active_members", "total_unique_ytd"):
            if a.get(k):
                return fmt_int(a[k])
        return None

    def perf_anchor(self) -> Optional[str]:
        v, c = self.perf.get("views"), self.perf.get("calls")
        if v is None or c is None:
            return None
        days = self.perf.get("window_days", 30)
        return f"{fmt_int(v)} profile views and {fmt_int(c)} calls in the last {days} days"

    def weakest_vs_peer(self) -> Optional[Tuple[str, str, str]]:
        """(label, mine, peer) for the metric furthest below peer average."""
        pairs = [
            ("ctr", "avg_ctr", True),
            ("calls", "avg_calls_30d", False),
            ("views", "avg_views_30d", False),
            ("directions", "avg_directions_30d", False),
        ]
        worst = None
        for mk, pk, is_ratio in pairs:
            mine, peer = self.perf.get(mk), self.peer.get(pk)
            if mine is None or not peer:
                continue
            gap = (float(mine) - float(peer)) / float(peer)
            if gap < 0 and (worst is None or gap < worst[0]):
                fmt = pct1 if is_ratio else fmt_int
                worst = (gap, METRIC_LABEL.get(mk, mk), fmt(mine), fmt(peer))
        return worst[1:] if worst else None

    def strongest_vs_peer(self) -> Optional[Tuple[str, str, str]]:
        pairs = [("ctr", "avg_ctr", True), ("calls", "avg_calls_30d", False),
                 ("views", "avg_views_30d", False)]
        best = None
        for mk, pk, is_ratio in pairs:
            mine, peer = self.perf.get(mk), self.peer.get(pk)
            if mine is None or not peer:
                continue
            gap = (float(mine) - float(peer)) / float(peer)
            if gap > 0 and (best is None or gap > best[0]):
                fmt = pct1 if is_ratio else fmt_int
                best = (gap, METRIC_LABEL.get(mk, mk), fmt(mine), fmt(peer))
        return best[1:] if best else None

    def peer_phrase(self) -> str:
        scope = self.peer.get("scope") or ""
        return "metro " + self.words["peers"] if "metro" in scope else "peer " + self.words["peers"]

    def top_trend(self, skip: Tuple[str, ...] = ()) -> Optional[dict]:
        ts = [t for t in (self.cat.get("trend_signals") or [])
              if t.get("query") and not any(k in t["query"] for k in skip)]
        if not ts:
            return None
        return sorted(ts, key=lambda t: -float(t.get("delta_yoy") or 0))[0]

    def season_beat(self, keywords: Tuple[str, ...] = ()) -> Optional[dict]:
        beats = [b for b in (self.cat.get("seasonal_beats") or [])
                 if not keywords or any(k in (b.get("note", "") + b.get("month_range", "")).lower() for k in keywords)]
        idx = self.now.month - 1
        for b in beats:
            if _month_range_contains(b.get("month_range", ""), idx):
                return b
        # next upcoming beat
        for step in range(1, 12):
            for b in beats:
                if _month_range_contains(b.get("month_range", ""), (idx + step) % 12):
                    return b
        return None

    def review_theme(self, sentiment: str) -> Optional[dict]:
        themes = [r for r in (self.m.get("review_themes") or []) if r.get("sentiment") == sentiment]
        if not themes:
            return None
        return sorted(themes, key=lambda r: -int(r.get("occurrences_30d") or 0))[0]

    def digest(self, item_id: Optional[str] = None, kinds: Tuple[str, ...] = ()) -> Optional[dict]:
        items = self.cat.get("digest") or []
        if item_id:
            for d in items:
                if d.get("id") == item_id:
                    return d
        for k in kinds:
            for d in items:
                if d.get("kind") == k:
                    return d
        return None

    def fix_plan(self) -> Tuple[str, str, str]:
        """(problem sentence, action phrase for CTA, accept body) — highest-value fix."""
        offer, live = self.lead_offer()
        ident = self.m.get("identity") or {}
        stale = self.signal_value("stale_posts")
        post_freq = self.peer.get("avg_post_freq_days")
        if not live and offer:
            return (
                f"You have no live offer on your listing right now — {self.peer_phrase()} lead with service+price offers like '{offer}'.",
                f"put '{offer}' live with a Google post",
                f"Starting now. Drafted for your approval:\n1) Live offer: '{offer}'\n2) Google post: \"{offer} at {self.name}, {self.locality or self.city}. Walk in or call to book.\"\nReply CONFIRM and I'll publish both today.",
            )
        if ident.get("verified") is False:
            return (
                f"{self.name} is still unverified on Google, which limits how often it shows in local search.",
                "start your Google verification today",
                "Starting the verification request now. Google will offer a postcard or phone-call check — the code comes to you, and I'll walk you through entering it. Reply CONFIRM and I'll submit the request.",
            )
        if stale:
            freq = f" ({self.peer_phrase()} post every {post_freq} days)" if post_freq else ""
            return (
                f"Your last Google post was {stale.replace('d', ' days')} ago{freq}.",
                "draft 3 fresh Google posts",
                f"Drafting now — 3 Google posts around '{offer}' and your top-rated services. You'll get them here for review; reply CONFIRM to publish all three." if offer else
                "Drafting now — 3 Google posts around your top services. You'll get them here for review; reply CONFIRM to publish all three.",
            )
        weak = self.weakest_vs_peer()
        if weak:
            label, mine, peer = weak
            return (
                f"Your {label}: {mine} vs {peer} for {self.peer_phrase()}.",
                "refresh your listing photos, description and offer",
                f"On it. Refresh plan ready: new cover photo, updated description, and '{offer}' pinned as the lead offer. Reply CONFIRM and I'll push the changes today." if offer else
                "On it. Refresh plan ready: new cover photo, updated description and a fresh Google post. Reply CONFIRM and I'll push the changes today.",
            )
        return (
            "",
            f"draft a Google post around '{offer}'" if offer else "draft a fresh Google post",
            f"Drafting it now — a Google post featuring '{offer}'. You'll see it here first; reply CONFIRM to publish." if offer else
            "Drafting it now — a fresh Google post. You'll see it here first; reply CONFIRM to publish.",
        )

    # --- customer helpers -------------------------------------------------
    def cust_names(self) -> Tuple[Optional[str], Optional[str]]:
        """(addressee, subject). 'Aanya (parent: Sneha)' -> ('Sneha', 'Aanya')."""
        raw = (g(self.c, "identity", "name") or "").strip()
        if not raw or raw.startswith("("):
            return None, None
        m = re.match(r"^(.*?)\s*\(parent:\s*(.*?)\)\s*$", raw)
        if m:
            return m.group(2).strip(), m.group(1).strip()
        return raw, raw

    def cust_lang(self) -> str:
        pref = str(g(self.c, "identity", "language_pref") or "en").lower()
        if pref in ("hi", "hindi"):
            return "hi"
        if "hi" in pref.split("-") or pref.startswith("hi"):
            return "hinglish"
        return "en"

    def regional_greeting(self) -> Optional[str]:
        pref = str(g(self.c, "identity", "language_pref") or "").lower()
        if pref.startswith("ta"):
            return "Vanakkam"
        if pref.startswith("te"):
            return "Namaskaram"
        if pref.startswith("kn"):
            return "Namaskara"
        return None

    def c_hi(self, en: str, hing: str) -> str:
        return hing if self.cust_lang() in ("hi", "hinglish") else en

    def sender_line(self) -> str:
        """How the merchant signs a customer-facing message."""
        if self.slug == "dentists" or not self.owner:
            return f"{self.name} here"
        return f"{self.owner} from {self.name} here"


# ---------------------------------------------------------------------------
# Merchant-facing handlers
# ---------------------------------------------------------------------------

DIGEST_KINDS = {
    "research_digest": ("research", "tech", "trend"),
    "research_digest_release": ("research", "tech", "trend"),
    "category_research_digest_release": ("research", "tech", "trend"),
    "regulation_change": ("compliance",),
    "compliance_alert": ("compliance",),
    "supply_alert": ("alert", "supply"),
    "cde_opportunity": ("cde",),
    "category_trend_movement": ("trend",),
}


def _patient_content_for(ctx: Ctx, item: dict) -> Optional[dict]:
    lib = ctx.cat.get("patient_content_library") or []
    if not lib:
        return None
    words = set(re.findall(r"[a-z]{4,}", (item.get("title", "") + " " + item.get("summary", "")).lower()))
    best, best_score = None, 0
    for pc in lib:
        pw = set(re.findall(r"[a-z]{4,}", (pc.get("title", "") + " " + pc.get("body", "")).lower()))
        score = len(words & pw)
        if score > best_score:
            best, best_score = pc, score
    return best if best_score >= 4 else None


def h_digest(ctx: Ctx) -> Draft:
    kinds = DIGEST_KINDS.get(ctx.t.get("kind"), ("research",))
    item_id = ctx.p.get("top_item_id") or ctx.p.get("digest_item_id") or ctx.p.get("alert_id")
    item = ctx.digest(item_id, kinds) or ctx.digest(None, ("research", "tech", "trend", "compliance", "alert", "supply", "cde"))
    if not item:
        return h_generic(ctx)
    kind = item.get("kind")
    title, source = item.get("title", ""), item.get("source", "")
    summary, actionable = item.get("summary", ""), item.get("actionable", "")
    facts = {"digest_id": item.get("id"), "source": source}

    if kind in ("alert", "supply") and ctx.t.get("kind") == "supply_alert" or kind == "alert":
        molecule = ctx.p.get("molecule") or ""
        batches = ctx.p.get("affected_batches") or []
        mfr = ctx.p.get("manufacturer")
        batch_txt = f"batches {join_list(batches)}" if batches else "specific batches"
        mfr_txt = f" by {mfr}" if mfr else ""
        mol_txt = molecule or "the affected molecule"
        cohort = ctx.cohort_line()
        core = [
            f"{source} — voluntary recall on {mol_txt} {batch_txt}{mfr_txt}.",
            first_sentence(summary.replace("Two batches (numbers in alert) flagged", "Flagged")),
        ]
        if "No safety risk" in summary or "no safety risk" in summary.lower():
            core.append("No safety risk beyond reduced effect, but customers should be told and offered a replacement.")
        if cohort:
            core.append(f"Worth checking {cohort} for anyone dispensed these batches.")
        cta_line = ctx.hi(
            f"Want me to filter your repeat-Rx list for {mol_txt} and draft the customer WhatsApp? Reply YES.",
            f"Main aapki repeat-Rx list se {mol_txt} wale customers filter karke WhatsApp draft kar doon? Reply YES.")
        accept = (f"Doing it now. Draft for affected customers:\n\n\"Namaste, {ctx.name} here. A voluntary recall covers some {mol_txt} "
                  f"{batch_txt}. If your pack matches, please bring it in and we'll arrange a replacement. Reply here if you're unsure — "
                  f"we'll check the batch for you.\"\n\nReply CONFIRM and I'll send it to the matching customers and set up the replacement-pickup list.")
        return Draft(core=core, cta_line=cta_line, rationale=(
            f"Urgent supply alert ({source}); batch numbers from trigger payload; anchored on merchant's repeat-Rx base; "
            f"single YES CTA offering to do the filtering + customer note."),
            accept_body=accept, detail=f"{title}. {summary} {actionable}".strip(),
            topic=f"the {mol_txt} recall", facts=facts)

    if kind == "compliance":
        deadline = parse_dt(ctx.p.get("deadline_iso")) or parse_dt(item.get("effective_date"))
        if not deadline:
            m = re.search(r"effective (\d{4}-\d{2}-\d{2})", title)
            deadline = parse_dt(m.group(1)) if m else None
        dleft = days_between(ctx.now, deadline)
        when = ""
        if deadline:
            when = f"Deadline: {fmt_date(deadline)} {deadline.year}"
            if dleft is not None and dleft > 0:
                when += f" — {dleft} days from now"
            when += "."
        core = [f"compliance heads-up from {source}: {title}.", summary]
        if when:
            core.append(when)
        cta_line = ctx.hi(
            f"Want a 3-step audit checklist for your {ctx.words['biz']} so you're covered well before the deadline? Reply YES.",
            f"Aapke {ctx.words['biz']} ke liye 3-step audit checklist bana doon, taaki deadline se pehle sab covered ho? Reply YES.")
        accept = (f"Here's your checklist for \"{title}\":\n1) {actionable or 'Review the change against your current setup'}\n"
                  f"2) Note which equipment/process is affected and what needs replacing\n3) Record the check (date + who did it) in your SOP file\n"
                  f"Reply CONFIRM and I'll set a reminder 30 days before the deadline.")
        return Draft(core=core, cta_line=cta_line, rationale=(
            f"Regulation change ({source}) with a hard deadline; loss-aversion framing via days remaining; "
            f"effort externalised with a ready checklist; single YES CTA."),
            accept_body=accept, detail=f"{summary} {actionable}".strip(), topic="the compliance checklist", facts=facts)

    if kind == "cde":
        dt = parse_dt(item.get("date"))
        credits = ctx.p.get("credits") or item.get("credits")
        fee = ctx.p.get("fee")
        bits = []
        if dt:
            bits.append(f"{fmt_date(dt, weekday=True)}, {fmt_time(dt)}")
        if credits:
            bits.append(f"{credits} CDE credits")
        if fee == "free_for_members":
            bits.append("free for IDA members")
        core = [f"{title} ({source}).", " · ".join(b for b in bits if b) + ".", summary]
        if actionable:
            core.append(actionable + ".")
        cta_line = ctx.hi("Want me to save your seat and send a reminder 2 hours before? Reply YES.",
                          "Seat save karke 2 ghante pehle reminder bhej doon? Reply YES.")
        accept = (f"Done — noted for {fmt_date(dt, weekday=True) if dt else 'the session date'}: {title}. "
                  f"Registration details are on the way here, and I'll ping you 2 hours before it starts. Reply CONFIRM to lock it in.")
        return Draft(core=core, cta_line=cta_line, rationale=(
            "CDE opportunity from category digest; date, credits and fee cited; low-effort YES CTA (Vera handles registration + reminder)."),
            accept_body=accept, detail=f"{summary} {actionable}".strip(), topic="the CDE session", facts=facts)

    # research / tech / trend / seasonal
    trial = item.get("trial_n")
    seg = item.get("patient_segment")
    cohort = None
    if seg and "high_risk" in str(seg) and ctx.agg.get("high_risk_adult_count"):
        cohort = f"your {fmt_int(ctx.agg['high_risk_adult_count'])} high-risk adult patients"
    elif seg and any("high_risk" in s for s in ctx.signals):
        cohort = "your high-risk adult patients"
    head = f"new from {source}: {title}" if kind == "research" else f"{title} ({source})"
    core = [head + "."]
    s1 = first_sentence(summary)
    if trial and str(fmt_int(trial)) not in s1:
        s1 = s1.rstrip(".") + f" (n={fmt_int(trial)})."
    core.append(s1)
    if cohort:
        core.append(f"Directly relevant to {cohort}.")
    elif actionable:
        core.append(actionable.rstrip(".") + ".")
    pc = _patient_content_for(ctx, item)
    people = ctx.words["people"][:-1]
    if pc or kind == "research":
        cta_line = ctx.hi(f"Want the 2-min summary plus a {people}-friendly WhatsApp you can forward? Reply YES.",
                          f"2-min summary aur ek {people}-friendly WhatsApp draft bhej doon, jo aap forward kar sakein? Reply YES.")
        draft = (f"{pc.get('title')} — {first_sentence(pc.get('body', ''))}" if pc else
                 f"New research ({source.split(',')[0]}): {first_sentence(summary)}")
        accept = (f"Sending the summary: {s1}\n\nForward-ready draft for your {ctx.words['people']}:\n\"{draft} "
                  f"Not sure if this applies to you? Message us and we'll check at your next visit.\"\n\n"
                  f"Reply CONFIRM and I'll also schedule it as a Google post for tomorrow 10am.")
    elif actionable and actionable.lower().startswith("worth"):
        cta_line = ctx.hi(f"Want a 1-page pilot plan for your {ctx.words['biz']} so you can decide quickly? Reply YES.",
                          f"Aapke {ctx.words['biz']} ke liye 1-page pilot plan bana doon, taaki jaldi decide kar sakein? Reply YES.")
        accept = (f"On it — a 1-page pilot plan: what it costs per the note, who it suits ({actionable.rstrip('.').lower()}), "
                  f"and how to test it for 4 weeks. I'll share it here; reply CONFIRM to start the pilot.")
    elif actionable:
        step = actionable.rstrip(".")
        cta_line = ctx.hi("Want me to take care of the first step for you? Reply YES.",
                          "Pehla step main handle kar doon? Bas YES reply karein.")
        accept = (f"On it. First step: {step[:1].lower() + step[1:]}. I'll prepare it and share it here for your OK — "
                  f"reply CONFIRM to go ahead.")
    else:
        cta_line = ctx.hi("Want the 2-min summary? Reply YES.", "2-min summary bhej doon? Reply YES.")
        accept = f"Here's the short version: {summary} Reply CONFIRM if you'd like me to act on it."
    return Draft(core=core, cta_line=cta_line, rationale=(
        f"Weekly digest item ({source}) chosen for this category; "
        f"{'tied to the merchant cohort from customer_aggregate; ' if cohort else ''}"
        "curiosity + reciprocity (Vera does the drafting); single YES CTA."),
        accept_body=accept, detail=f"{summary} {actionable}".strip(), topic=f"the {source.split(',')[0]} item", facts=facts)


def _metric_from_payload_or_data(ctx: Ctx, want_negative: bool) -> Tuple[Optional[str], Optional[float]]:
    metric, delta = ctx.p.get("metric"), ctx.p.get("delta_pct")
    if metric and delta is not None:
        return metric, float(delta)
    d7 = ctx.perf.get("delta_7d") or {}
    cands = [(k.replace("_pct", ""), float(v)) for k, v in d7.items() if isinstance(v, (int, float))]
    if not cands:
        return None, None
    cands.sort(key=lambda kv: kv[1], reverse=not want_negative)
    return cands[0]


def h_perf(ctx: Ctx) -> Draft:
    kind = ctx.t.get("kind")
    dip = kind in ("perf_dip",)
    metric, delta = _metric_from_payload_or_data(ctx, want_negative=dip)
    label = METRIC_LABEL.get(metric or "", humanize(metric or "activity"))
    window = ctx.p.get("window", "7d").replace("7d", "this week")
    base = ctx.p.get("vs_baseline")
    problem, action, accept = ctx.fix_plan()
    core: List[str] = []
    if dip:
        if delta is not None and delta < 0:
            line = f"your {label} dropped {pct(delta)} {window}"
            if base:
                line += f" against a baseline of ~{fmt_int(base)}"
            core.append(line + ".")
        else:
            weak = ctx.weakest_vs_peer()
            if weak:
                core.append(f"your {weak[0]} {is_are(weak[0])} trailing — {weak[1]} vs {weak[2]} for {ctx.peer_phrase()}.")
            else:
                core.append(f"flagging a softer patch on your listing this week ({ctx.perf_anchor()}).")
        if ctx.perf_anchor() and delta is not None and delta < 0:
            core.append(f"Last {ctx.perf.get('window_days', 30)} days: {ctx.perf_anchor().split(' in the last')[0]}.")
        if problem:
            core.append(problem)
        cta_line = ctx.hi(f"Want me to {action}? Reply YES — takes 5 min on your side.",
                          f"Want me to {action}? Bas YES reply karein — aapka sirf 5 min lagega.")
        rat = (f"Performance dip ({label} {pct(delta, True) if delta is not None else 'n/a'}) from "
               f"{'trigger payload' if ctx.p.get('metric') else 'merchant delta_7d'}; diagnosis from merchant state "
               f"(offers/verification/posts/peer gap); loss aversion + one fix as single YES CTA.")
        return Draft(core=core, cta_line=cta_line, rationale=rat, accept_body=accept,
                     detail=problem or (ctx.perf_anchor() or ""), topic="the dip fix")

    # spike
    if delta is not None and delta > 0:
        line = f"your {label} {is_are(label)} up {pct(delta)} {window}"
        if base:
            line += f" (baseline ~{fmt_int(base)})"
        driver = ctx.p.get("likely_driver")
        if driver:
            line += f" — likely driven by your {humanize(driver)}"
        core.append(line + ".")
    else:
        core.append(f"your listing is holding up well — {ctx.perf_anchor()}.")
    strong = ctx.strongest_vs_peer()
    if strong:
        core.append(f"You're ahead of {ctx.peer_phrase()} on {strong[0]}: {strong[1]} vs {strong[2]}.")
    offer, live = ctx.lead_offer()
    if offer:
        core.append(f"Best time to push {'your' if live else 'an offer like'} '{offer}' while attention is high.")
    cta_line = ctx.hi("Want me to draft a follow-up Google post to ride the momentum? Reply YES.",
                      "Momentum ka fayda uthane ke liye ek follow-up Google post draft kar doon? Reply YES.")
    accept = (f"Drafting now — a follow-up post{' featuring ' + repr(offer) if offer else ''} timed for this week's peak. "
              f"You'll see it here first; reply CONFIRM to publish.")
    return Draft(core=core, cta_line=cta_line, rationale=(
        "Performance spike; reinforces what's working with the merchant's own numbers + peer comparison; "
        "single YES CTA to capitalise on momentum."), accept_body=accept, detail=ctx.perf_anchor() or "",
        topic="the follow-up post")


def h_seasonal_dip(ctx: Ctx) -> Draft:
    metric, delta = _metric_from_payload_or_data(ctx, want_negative=True)
    label = METRIC_LABEL.get(metric or "", humanize(metric or "activity"))
    beat = ctx.season_beat()
    dig = ctx.digest(None, ("seasonal",))
    core = []
    if delta is not None and delta < 0:
        core.append(f"your {label} {is_are(label)} down {pct(delta)} this week — and that's expected, not a problem.")
    else:
        core.append("a seasonal slowdown window is on for your category right now.")
    if beat:
        core.append(f"{beat.get('month_range')} is the {beat.get('note')}.")
    if dig and dig.get("actionable"):
        core.append(f"Playbook: {dig['actionable'].rstrip('.')}.")
    cohort = ctx.cohort_line()
    if cohort:
        core.append(f"Best use of this window: keep {cohort} engaged.")
    cta_line = ctx.hi("Want me to draft a 4-week retention challenge you can announce this week? Reply YES.",
                      "Is hafte announce karne ke liye 4-week retention challenge draft kar doon? Reply YES.")
    accept = ("Drafted: '30-Day Consistency Challenge' — attend 12 sessions in 4 weeks, get your name on the wall + a free body-composition check. "
              "WhatsApp announcement + Google post ready for review. Reply CONFIRM to send to your members.") if ctx.slug == "gyms" else (
              "Drafted a 4-week loyalty push for your regulars (WhatsApp note + Google post). Reply CONFIRM to send.")
    return Draft(core=core, cta_line=cta_line, rationale=(
        "Expected seasonal dip — anxiety pre-emption using category seasonal_beats/digest; reframes to retention "
        "with merchant's own member base; single YES CTA."), accept_body=accept,
        detail=(beat or {}).get("note", ""), topic="the retention challenge")


def h_renewal(ctx: Ctx) -> Draft:
    sub = ctx.m.get("subscription") or {}
    days = ctx.p.get("days_remaining", sub.get("days_remaining"))
    plan = ctx.p.get("plan") or sub.get("plan") or "magicpin"
    amount = ctx.p.get("renewal_amount")
    core = []
    trial = str(plan).lower() == "trial" or sub.get("status") == "trial"
    if trial:
        line = f"your trial ends in {days} days" if days else "your trial is ending"
    elif days and int(days) > 45:
        line = f"your {plan} plan runs another {days} days, and the early-renewal window is open now"
    else:
        line = f"your {plan} plan ends in {days} days" if days not in (None, 0) else f"your {plan} plan is due for renewal"
    if amount:
        line += f" (renewal {rupees(amount)})"
    core.append(line + ".")
    anchor = ctx.perf_anchor()
    if anchor:
        core.append(f"What it's delivering right now: {anchor}.")
    metric, delta = _metric_from_payload_or_data(ctx, want_negative=True)
    if delta is not None and delta <= -0.15:
        core.append(f"{METRIC_LABEL.get(metric, metric).capitalize()} {is_are(METRIC_LABEL.get(metric, metric))} down {pct(delta)} this week — a lapse now would make that worse, not better.")
    if trial:
        cta_line = ctx.hi("Want me to move you to Pro before the trial ends, so nothing pauses? Reply YES.",
                          "Trial khatam hone se pehle Pro pe shift kar doon, taaki kuch pause na ho? Reply YES.")
    else:
        cta_line = ctx.hi("Want me to renew it now so there's no gap in your listing upkeep? Reply YES.",
                          "Renewal abhi kar doon taaki listing upkeep mein koi gap na aaye? Reply YES.")
    accept = (f"Processing the {'Pro upgrade' if trial else plan + ' renewal'}{' for ' + rupees(amount) if amount else ''}. I'll share the payment request right here — "
              f"reply CONFIRM once it's paid and I'll confirm the new end date.")
    return Draft(core=core, cta_line=cta_line, rationale=(
        "Renewal due; days/amount from trigger, value proof from merchant's own 30-day numbers; loss aversion; single YES CTA."),
        accept_body=accept, detail=anchor or "", topic="the renewal")


def h_winback_merchant(ctx: Ctx) -> Draft:
    sub = ctx.m.get("subscription") or {}
    days = ctx.p.get("days_since_expiry", sub.get("days_since_expiry"))
    dip = ctx.p.get("perf_dip_pct")
    lapsed = ctx.p.get("lapsed_customers_added_since_expiry")
    core = [f"it's been {days} days since your magicpin plan lapsed." if days else "your magicpin plan is currently inactive."]
    since = []
    if dip is not None:
        since.append(f"calls down {pct(dip)}")
    if lapsed:
        since.append(f"{fmt_int(lapsed)} more {ctx.words['people']} gone quiet")
    if since:
        core.append("Since then: " + join_list(since) + ".")
    elif ctx.perf_anchor():
        core.append(f"Your listing still pulled {ctx.perf_anchor()} — demand is there.")
    weak = ctx.weakest_vs_peer()
    if weak:
        core.append(f"Your {weak[0]}: {weak[1]} vs {weak[2]} for {ctx.peer_phrase()}.")
    cta_line = ctx.hi("Want me to reactivate and restart your profile posts this week? Reply YES.",
                      "Plan reactivate karke is hafte se profile posts restart kar doon? Reply YES.")
    accept = ("Reactivation started. I'll share the payment request here; once it's through, I'll publish a fresh Google post "
              "and re-pin your best offer within 24 hours. Reply CONFIRM to proceed.")
    return Draft(core=core, cta_line=cta_line, rationale=(
        "Win-back of lapsed subscriber: days since expiry + measurable loss since then (payload/merchant data); "
        "loss aversion; single YES CTA."), accept_body=accept, detail=" ".join(core[1:]), topic="reactivation")


def h_dormant(ctx: Ctx) -> Draft:
    days = ctx.p.get("days_since_last_merchant_message")
    trend = ctx.top_trend()
    core = []
    if days:
        core.append(f"it's been {days} days since we last spoke, so here's one useful thing.")
    else:
        core.append("one quick, useful thing for you this week.")
    if trend:
        core.append(f"'{trend['query']}' searches are up {pct(trend.get('delta_yoy'))} year-on-year.")
    anchor = ctx.perf_anchor()
    if anchor:
        core.append(f"Your listing got {anchor}.")
    problem, action, accept = ctx.fix_plan()
    if problem:
        core.append(problem)
    sub = ctx.m.get("subscription") or {}
    if sub.get("status") == "expired":
        core.append("(Your plan is paused, so this one's on the house.)")
    cta_line = ctx.hi(f"Want me to {action}? Reply YES.", f"Want me to {action}? Bas YES reply karein, baaki main sambhal lungi.")
    return Draft(core=core, cta_line=cta_line, rationale=(
        "Dormant merchant — lead with value (category trend + their own numbers) instead of a reminder; "
        "one concrete fix; single YES CTA."), accept_body=accept, detail=problem, topic=action)


def h_curious(ctx: Ctx) -> Draft:
    trend = ctx.top_trend(skip=("offer", "price", "cost", "near me delivery"))
    hi = f"Hi {ctx.owner}!" if ctx.owner and ctx.slug != "dentists" else f"{ctx.sal},"
    ask = {
        "dentists": "which treatment have patients asked about most this week",
        "salons": f"which service has been most asked-for at {ctx.name} this week",
        "restaurants": "which dish has been your top order this week",
        "gyms": "which class or plan are walk-ins asking about most this week",
        "pharmacies": "which product have customers asked for most this week",
    }.get(ctx.slug, "what customers have asked for most this week")
    core = [f"quick question — {ask}?"]
    if trend:
        core.append(f"My guess from search data: '{trend['query']}' (up {pct(trend.get('delta_yoy'))} YoY).")
    core.append("Tell me and I'll turn it into a Google post plus a ready WhatsApp reply for price enquiries.")
    cta_line = ctx.hi("Just reply with the name — 5 min, main baaki sambhal lungi.",
                      "Just reply with the name — 5 min, main baaki sambhal lungi.") if ctx.hinglish else "Just reply with the name — takes 5 min."
    accept = ("Great — send me the service name and a price if you have one; I'll draft the Google post + WhatsApp reply right here "
              "for you to CONFIRM.")
    return Draft(core=core, cta_line=cta_line, cta="open_ended", opening=hi, rationale=(
        "Curious-ask cadence: asking-the-merchant lever with a data-backed guess (category trend_signals); "
        "reciprocity (Vera drafts post + reply); open-ended CTA."), accept_body=accept,
        detail="", topic="this week's most-asked service")


def h_festival(ctx: Ctx) -> Draft:
    fest = ctx.p.get("festival")
    date = parse_dt(ctx.p.get("date"))
    offer, live = ctx.lead_offer()
    core = []
    if fest:
        dleft = days_between(ctx.now, date)
        when = f" on {fmt_date(date)}" if date else ""
        if dleft is not None and dleft > 0:
            when += f" — {dleft} days out"
        core.append(f"{fest} is{when}.")
        beat = None
        for b in ctx.cat.get("seasonal_beats") or []:
            if date and _month_range_contains(b.get("month_range", ""), date.month - 1):
                beat = b
                break
        if beat:
            core.append(f"For {ctx.words['peers']}, {beat['month_range']} is the {beat['note']}.")
        if dleft is not None and dleft > 60:
            core.append("Early is the edge: set the package now, while slots are still open.")
    else:
        beat = ctx.season_beat(keywords=("festival", "festive", "diwali", "wedding", "christmas"))
        if beat:
            core.append(f"festive window ahead — for {ctx.words['peers']}, {beat['month_range']} is the {beat['note']}.")
        else:
            core.append("the festive window is coming up.")
    if offer:
        core.append(f"Your live '{offer}' can anchor a festive package." if live else f"An offer like '{offer}' can anchor a festive package.")
    fname = fest or "Festive"
    cta_line = ctx.hi(f"Want me to draft a {fname} package + Google post for you to review? Reply YES.",
                      f"{fname} package + Google post draft kar doon, aap review kar lena? Reply YES.")
    accept = (f"Drafting now: '{fname} Special' built around '{offer}', with a Google post and a WhatsApp note for your regulars. "
              f"You'll get both here; reply CONFIRM to publish.") if offer else (
              f"Drafting now: a {fname} package with a Google post and WhatsApp note. Reply CONFIRM to publish once you've reviewed.")
    return Draft(core=core, cta_line=cta_line, rationale=(
        "Festival trigger; date/lead time from payload and the category's seasonal beat; anchored on the merchant's own offer; "
        "single YES CTA."), accept_body=accept, detail=" ".join(core), topic=f"the {fname} package")


def h_ipl(ctx: Ctx) -> Draft:
    match, venue = ctx.p.get("match", "tonight's match"), ctx.p.get("venue")
    mt = parse_dt(ctx.p.get("match_time_iso"))
    weeknight = ctx.p.get("is_weeknight")
    dig = ctx.digest("d_2026W17_ipl_window", ("seasonal",))
    offer, live = ctx.lead_offer()
    when = ""
    if mt:
        same_day = ctx.now.astimezone(mt.tzinfo).date() == mt.date()
        when = ("tonight " if same_day else f"{fmt_date(mt, weekday=True)}, ") + fmt_time(mt)
    core = [f"{match}{' at ' + venue if venue else ''}{', ' + when if when else ''}."]
    if weeknight is False:
        if dig:
            core.append(f"Heads-up: {first_sentence(dig.get('summary', '')).rstrip('.')} ({dig.get('source')}).")
        core.append(f"So skip a dine-in match promo today — push {'your ' + repr(offer) if offer else 'a combo'} as a delivery-first special instead.")
        action = "draft the delivery banner + an Insta story"
    else:
        if dig:
            core.append(f"Weeknight matches have been driving covers up ({dig.get('source')}).")
        cat_combo = next((o["title"] for o in ctx.cat.get("offer_catalog") or [] if "Match" in o.get("title", "")), None)
        core.append(f"Good night for {'your ' + repr(offer) if offer else repr(cat_combo) if cat_combo else 'a match-night combo'} as a match-night special.")
        action = "set up the match-night post"
    cta_line = ctx.hi(f"Want me to {action}? Live in 10 min — reply YES.", f"Want me to {action}? 10 min mein live — bas YES reply karein.")
    accept = (f"On it — drafting {action.replace('draft ', '')} around {repr(offer) if offer else 'your match special'}. "
              f"Preview comes here in 10 minutes; reply CONFIRM to publish.")
    return Draft(core=core, cta_line=cta_line, rationale=(
        "IPL match trigger; uses category digest (Saturday vs weeknight covers) to give a contrarian, data-backed call; "
        "leverages merchant's existing offer; single YES CTA with 10-min effort cap."),
        accept_body=accept, detail=(dig or {}).get("summary", ""), topic="the match-day promo")


def h_review_theme(ctx: Ctx) -> Draft:
    theme = ctx.p.get("theme")
    occ = ctx.p.get("occurrences_30d")
    quote = ctx.p.get("common_quote")
    trend = ctx.p.get("trend")
    if not theme:
        neg = ctx.review_theme("neg")
        if neg:
            theme, occ, quote = neg.get("theme"), neg.get("occurrences_30d"), neg.get("common_quote")
    pos = ctx.review_theme("pos")
    core = []
    if theme:
        line = f"{fmt_int(occ) + ' reviews' if occ else 'Several reviews'} in the last 30 days mention {humanize(theme)}"
        if trend:
            line += f" (trend: {trend})"
        core.append(line + ".")
        if quote:
            core.append(f"One says: \"{quote}\".")
        if pos:
            core.append(f"Balance: {fmt_int(pos.get('occurrences_30d'))} praise {humanize(pos.get('theme'))} — that's your strength to lead with.")
        action = f"draft a calm public reply for the {humanize(theme)} reviews + one fix you can announce"
        accept = (f"Drafted reply for the {humanize(theme)} reviews:\n\"Thank you for flagging this — you're right, and we've changed how we handle it. "
                  f"Please give us another try and tell us how it goes.\"\nReply CONFIRM and I'll post it on the latest ones.")
    else:
        rating, count = ctx.peer.get("avg_rating"), ctx.peer.get("avg_review_count")
        core.append("new review activity on your listing this week.")
        if rating and count:
            core.append(f"{ctx.peer_phrase().capitalize()} average {rating}★ across {fmt_int(count)} reviews — replies within 24h keep ratings climbing.")
        action = "draft replies to your latest reviews"
        accept = "Drafting replies to your latest reviews now — you'll see them here. Reply CONFIRM to post."
    cta_line = ctx.hi(f"Want me to {action}? Reply YES.", f"Want me to {action}? Bas YES reply karein, baaki main sambhal lungi.")
    return Draft(core=core, cta_line=cta_line, rationale=(
        "Review theme emerged; count + verbatim quote from trigger/merchant review_themes; balances with a positive theme; "
        "single YES CTA to externalise the reply work."), accept_body=accept, detail=" ".join(core), topic="the review replies")


MILESTONE_STEPS = [50, 100, 150, 200, 250, 500, 750, 1000, 1500, 2000, 2500, 5000, 10000]


def h_milestone(ctx: Ctx) -> Draft:
    metric = ctx.p.get("metric")
    now_v, target = ctx.p.get("value_now"), ctx.p.get("milestone_value")
    imminent = ctx.p.get("is_imminent")
    core = []
    if metric and now_v is not None and target:
        label = METRIC_LABEL.get(metric, humanize(metric))
        if imminent and float(now_v) < float(target):
            core.append(f"you're at {fmt_int(now_v)} {label} — just {fmt_int(float(target) - float(now_v))} away from {fmt_int(target)}.")
        else:
            core.append(f"you've crossed {fmt_int(target)} {label} ({fmt_int(now_v)} now).")
        peer_rc = ctx.peer.get("avg_review_count")
        if "review" in metric and peer_rc:
            core.append(f"{ctx.peer_phrase().capitalize()} average {fmt_int(peer_rc)} — you're {'ahead' if float(now_v) >= peer_rc else 'close'}.")
        action = "send a review-request WhatsApp to your recent regulars" if imminent else "draft a thank-you post for your listing"
    else:
        total = ctx.agg.get("total_unique_ytd") or ctx.agg.get("total_active_members")
        step = max([s for s in MILESTONE_STEPS if total and s <= float(total)] or [0])
        if total and step:
            core.append(f"milestone: {fmt_int(total)} unique {ctx.words['people']} this year — past the {fmt_int(step)} mark.")
        elif ctx.perf.get("views"):
            core.append(f"milestone: {fmt_int(ctx.perf['views'])} profile views in the last 30 days.")
        else:
            core.append("you've hit a new milestone on your listing.")
        action = "draft a thank-you Google post that turns it into social proof"
    core.append("Milestones make great social proof when shared the same week.")
    cta_line = ctx.hi(f"Want me to {action}? Reply YES.", f"Want me to {action}? Bas YES reply karein, baaki main sambhal lungi.")
    accept = (f"Drafting now. Post: \"Thank you, {ctx.locality or ctx.city}! {core[0].split('—')[0].replace('you', 'we').strip().rstrip('.').capitalize()}. "
              f"Here's to the next one.\" Reply CONFIRM to publish.")
    return Draft(core=core, cta_line=cta_line, rationale=(
        "Milestone trigger; exact figures from payload or merchant aggregate (rounded down to a real crossed mark); "
        "social-proof lever; single YES CTA."), accept_body=accept, detail=core[0], topic="the milestone post")


def _price_of(title: str) -> Optional[int]:
    m = re.search(r"₹\s?([\d,]+)", title or "")
    return int(m.group(1).replace(",", "")) if m else None


def h_planning(ctx: Ctx) -> Draft:
    topic = humanize(ctx.p.get("intent_topic") or "your plan")
    last = ctx.p.get("merchant_last_message")
    offer, live = ctx.lead_offer()
    hist_vera = [h.get("body", "") for h in (ctx.m.get("conversation_history") or []) if h.get("from") == "vera"]
    core: List[str] = []
    opening = f"{ctx.sal}, here's a starter version — edit anything:"
    if ("corporate" in topic or "bulk" in topic) and offer and _price_of(offer):
        p = _price_of(offer)
        base_name = re.sub(r"\s*@.*$", "", offer)
        t1, t2, t3 = int(round(p * 0.93 / 5) * 5), int(round(p * 0.88 / 5) * 5), int(round(p * 0.83 / 5) * 5)
        core = [
            f"\n\n{ctx.name} Corporate {base_name.replace('Weekday Lunch ', '')} — for offices around {ctx.locality or ctx.city}",
            f"\n• 10+ orders: {rupees(t1)} each (retail {rupees(p)})",
            f"\n• 25+ orders: {rupees(t2)} each",
            f"\n• 50+ orders: {rupees(t3)} each + free delivery",
            "\n• Order by 5pm the day before; delivered 12:30–1pm",
            "\n\n",
        ]
        cta_line = ctx.hi("Want me to draft the WhatsApp pitch for office admins + a Google post? Reply YES.",
                          "Office admins ke liye WhatsApp pitch + Google post draft kar doon? Reply YES.")
        accept = (f"Here's the pitch:\n\"Hi! {ctx.name} now does corporate lunches in {ctx.locality or ctx.city} — "
                  f"from {rupees(t3)}/{base_name.split()[-1].lower()} for 50+, delivered 12:30–1pm. Order by 5pm the day before.\"\n"
                  f"Reply CONFIRM and I'll publish the Google post and share the pitch for forwarding.")
    elif hist_vera and any(k in hist_vera[-1].lower() for k in topic.split()):
        prev = hist_vera[-1]
        opening = f"{ctx.sal}, picking up from where we left off on the {topic}:"
        core = [f" {first_sentence(prev.split('—')[-1].strip()) if '—' in prev else first_sentence(prev)}",
                " " + re.sub(r"Want me to.*$", "", prev.split('. ', 1)[-1]).strip()]
        core = [c for c in core if c.strip()]
        core.append(" I've blocked out the launch as: week 1 announcement, week 2 first batch.")
        cta_line = ctx.hi("Want me to publish the GBP post + Insta carousel draft now? Reply YES.",
                          "GBP post + Insta carousel draft abhi publish kar doon? Reply YES.")
        accept = ("Publishing drafts now: GBP post + a 4-slide Insta carousel (program, schedule, price, how to book). "
                  "Reply CONFIRM once you've checked them and I'll make them live.")
    else:
        core = [f" {topic.capitalize()} — 1) what's included, 2) {'price anchored on ' + repr(offer) if offer else 'a clear service+price'}, "
                f"3) launch via a Google post + WhatsApp to your regulars."]
        cta_line = ctx.hi("Want me to write the full draft now? Reply YES.", "Poora draft abhi likh doon? Reply YES.")
        accept = f"Writing the full {topic} draft now — you'll have it here in a few minutes. Reply CONFIRM to publish once you've reviewed."
    rat = (f"Merchant already expressed intent ('{last}') — skip qualifying, go straight to a drafted artifact built from their "
           f"own offer/history; single YES CTA for the next action.") if last else (
           "Active planning intent: deliver a concrete draft immediately; single YES CTA.")
    return Draft(core=core, cta_line=cta_line, opening=opening, rationale=rat, accept_body=accept,
                 detail=" ".join(core), topic=topic)


def h_gbp_unverified(ctx: Ctx) -> Draft:
    uplift = ctx.p.get("estimated_uplift_pct")
    path = ctx.p.get("verification_path")
    core = [f"{ctx.name} is still unverified on Google."]
    if uplift:
        core.append(f"Verified listings see roughly {pct(uplift)} more customer actions.")
    anchor = ctx.perf_anchor()
    if anchor:
        core.append(f"You're already getting {anchor} unverified — that's the base the uplift applies to.")
    if path:
        core.append(f"Verification is via {humanize(path).replace(' or ', ' or ')} — about 5 minutes of your time.")
    cta_line = ctx.hi("Want me to start the verification request today? Reply YES.",
                      "Verification request aaj hi start kar doon? Reply YES.")
    accept = ("Starting the verification request now. Google will send a code by postcard or phone call — share it here "
              "when it arrives and I'll complete the rest. Reply CONFIRM to submit.")
    return Draft(core=core, cta_line=cta_line, rationale=(
        "Unverified GBP; uplift estimate + verification path from payload, merchant's own traffic as the base; "
        "effort externalised; single YES CTA."), accept_body=accept, detail=" ".join(core), topic="verification")


def h_competitor(ctx: Ctx) -> Draft:
    name, dist = ctx.p.get("competitor_name"), ctx.p.get("distance_km")
    their = ctx.p.get("their_offer")
    opened = parse_dt(ctx.p.get("opened_date"))
    offer, live = ctx.lead_offer()
    pos = ctx.review_theme("pos")
    core = []
    if name:
        line = f"heads-up — {name} opened{' ' + str(dist) + ' km away' if dist else ' nearby'}"
        if opened:
            line += f" on {fmt_date(opened)}"
        if their:
            line += f", leading with '{their}'"
        core.append(line + ".")
        if their and offer and live and _price_of(their) and _price_of(offer):
            core.append(f"Yours is '{offer}' — don't chase them on price.")
    else:
        core.append(f"a new {ctx.words['biz']} listing has opened near {ctx.locality or ctx.city}.")
    strong = ctx.strongest_vs_peer()
    if pos:
        q = pos.get("common_quote")
        core.append(f"Your edge: {fmt_int(pos.get('occurrences_30d'))} recent reviews praise {humanize(pos.get('theme'))}" +
                    (f" (\"{q}\")." if q else "."))
    elif strong:
        core.append(f"Your edge: {strong[0]} at {strong[1]} vs {strong[2]} for {ctx.peer_phrase()}.")
    else:
        weak = ctx.weakest_vs_peer()
        if weak:
            core.append(f"Watch-out: your {weak[0]}: {weak[1]} vs {weak[2]} for {ctx.peer_phrase()}.")
    cta_line = ctx.hi("Want me to draft a Google post that leads with your strengths this week? Reply YES.",
                      "Is hafte aapki strengths ko lead karta hua Google post draft kar doon? Reply YES.")
    accept = (f"Drafting now — a post that leads with {humanize(pos.get('theme')) if pos else 'what your ' + ctx.words['people'] + ' rate you for'}"
              f"{' and your ' + repr(offer) if offer and live else ''}. Preview here shortly; reply CONFIRM to publish.")
    return Draft(core=core, cta_line=cta_line, rationale=(
        "Competitor opened; only payload-provided competitor details are named; counters with merchant's own review/peer "
        "strengths rather than price; single YES CTA."), accept_body=accept, detail=" ".join(core), topic="the competitor response")


def h_category_seasonal(ctx: Ctx) -> Draft:
    trends = ctx.p.get("trends") or []
    parsed = []
    for tr in trends:
        m = re.match(r"(.+?)_demand_([+-]\d+)", str(tr))
        if m:
            parsed.append(f"{humanize(m.group(1))} {m.group(2)}%")
    dig = ctx.digest(None, ("seasonal",))
    core = []
    season = humanize(ctx.p.get("season", "the season")).replace(" 2026", "")
    if parsed:
        core.append(f"{season} demand shift is on: {join_list(parsed)}.")
    elif dig:
        core.append(f"{dig.get('title')}.")
    if dig and dig.get("actionable"):
        core.append(f"Shelf move: {dig['actionable'].rstrip('.')}.")
    count = ctx.audience_count()
    cta_line = ctx.hi(f"Want me to draft a '{season.title()} essentials' WhatsApp for your {count + ' ' if count else ''}{ctx.words['people']}? Reply YES.",
                      f"Aapke {count + ' ' if count else ''}{ctx.words['people']} ke liye '{season.title()} essentials' WhatsApp draft kar doon? Reply YES.")
    pc = next((p for p in ctx.cat.get("patient_content_library") or [] if "summer" in p.get("id", "") or "summer" in p.get("title", "").lower()), None)
    accept = (f"Draft ready:\n\"{pc.get('title')} — {first_sentence(pc.get('body', ''))} All in stock at {ctx.name}.\"\n"
              f"Reply CONFIRM and I'll send it.") if pc else "Drafting the WhatsApp now — reply CONFIRM to send once you've seen it."
    return Draft(core=core, cta_line=cta_line, rationale=(
        "Category seasonal demand shift with exact % from payload; concrete shelf action from digest; "
        "customer-outreach offer sized to merchant's base; single YES CTA."), accept_body=accept,
        detail=(dig or {}).get("summary", ""), topic="the seasonal WhatsApp")


def h_generic(ctx: Ctx) -> Draft:
    kind = humanize(ctx.t.get("kind") or "update")
    facts = []
    for k, v in (ctx.p or {}).items():
        if k in ("placeholder", "metric_or_topic", "category") or k.endswith("_id") or v in (None, "", [], {}):
            continue
        if isinstance(v, bool):
            continue
        if isinstance(v, float) and abs(v) < 1.5 and ("pct" in k or "delta" in k):
            facts.append(f"{humanize(k)} {pct(v, True)}")
        elif isinstance(v, (int, float)):
            facts.append(f"{humanize(k)} {fmt_int(v)}")
        elif isinstance(v, str):
            dt = parse_dt(v) if re.match(r"\d{4}-\d{2}-\d{2}", v) else None
            facts.append(f"{humanize(k)} {fmt_date(dt) if dt else humanize(v)}")
        elif isinstance(v, list) and all(isinstance(x, str) for x in v):
            facts.append(f"{humanize(k)}: {join_list([humanize(x) for x in v[:4]])}")
        if len(facts) >= 3:
            break
    core = [f"{kind} for {ctx.locality or ctx.city or ctx.name}" + (f" — {'; '.join(facts)}." if facts else ".")]
    problem, action, accept = ctx.fix_plan()
    anchor = ctx.perf_anchor()
    if anchor:
        core.append(f"Your listing: {anchor}.")
    if problem:
        core.append(problem)
    cta_line = ctx.hi(f"Want me to {action}? Reply YES.", f"Want me to {action}? Bas YES reply karein, baaki main sambhal lungi.")
    return Draft(core=core, cta_line=cta_line, rationale=(
        f"'{kind}' trigger; only payload facts + merchant's own numbers used; one concrete fix; single YES CTA."),
        accept_body=accept, detail=problem, topic=action)


# ---------------------------------------------------------------------------
# Customer-facing handlers (send_as = merchant_on_behalf)
# ---------------------------------------------------------------------------


CUST_SERVICE = {
    "dentists": ("dental check-up", "appointment", "routine dental follow-up"),
    "salons": ("regular salon visit", "appointment", "regular appointment"),
    "gyms": ("fitness check-in", "session", "plan renewal"),
    "restaurants": ("next visit", "table booking", "usual order"),
    "pharmacies": ("health check", "pickup", "regular medicine refill"),
}


def _cust_service(ctx: Ctx, idx: int) -> str:
    return CUST_SERVICE.get(ctx.slug, ("check-up", "appointment", "regular follow-up"))[idx]


def _cust_open(ctx: Ctx) -> str:
    addressee, _ = ctx.cust_names()
    lang = ctx.cust_lang()
    greet = ctx.regional_greeting()
    senior = g(ctx.c, "identity", "senior_citizen")
    via = str(g(ctx.c, "preferences", "channel") or "")
    emoji = ctx.words.get("emoji", "")
    if senior or "via_son" in via or "via_daughter" in via:
        return f"Namaste — {ctx.name}, {ctx.locality} {'yahan' if lang != 'en' else 'here'}."
    if greet and addressee:
        return f"{greet} {addressee}! {ctx.sender_line()} {emoji}".strip()
    if lang == "hi":
        return f"Namaste {addressee or 'ji'}! {ctx.sender_line()} {emoji}".strip()
    return f"Hi {addressee or 'there'}, {ctx.sender_line()} {emoji}".strip()


def _cust_offer(ctx: Ctx) -> Optional[str]:
    live = ctx.active_offers()
    return live[0] if live else None


def _slots(ctx: Ctx, key: str = "available_slots") -> List[str]:
    return [s.get("label") for s in (ctx.p.get(key) or []) if isinstance(s, dict) and s.get("label")]


def _slot_cta(ctx: Ctx, slots: List[str]) -> Tuple[str, str]:
    if len(slots) >= 2:
        opts = ", ".join(f"{i + 1} for {s}" for i, s in enumerate(slots[:3]))
        return ctx.c_hi(f"Reply {opts} — or tell us a time that suits you.",
                        f"Reply {opts} — ya jo time aapko suit kare, bata dijiye."), "multi_choice_slot"
    if len(slots) == 1:
        return ctx.c_hi(f"Reply YES to hold {slots[0]} for you.", f"{slots[0]} hold kar dein? Reply YES."), "binary_yes_no"
    return ctx.c_hi("Reply YES and we'll share the next open slots.",
                    "Reply YES kijiye, hum agle available slots bhej denge."), "binary_yes_no"


def _pref_slot_text(ctx: Ctx) -> Optional[str]:
    ps = g(ctx.c, "preferences", "preferred_slots")
    return " ".join(w.capitalize() if w in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
                    else w for w in humanize(ps).split()) if ps else None


def hc_recall(ctx: Ctx) -> Draft:
    _, subject = ctx.cust_names()
    service = humanize(ctx.p.get("service_due")).replace("6 month", "6-month") if ctx.p.get("service_due") else _cust_service(ctx, 0)
    last = parse_dt(ctx.p.get("last_service_date") or g(ctx.c, "relationship", "last_visit"))
    due = parse_dt(ctx.p.get("due_date"))
    slots = _slots(ctx)
    offer = _cust_offer(ctx)
    since = days_between(last, ctx.now)
    core = []
    own = not subject or subject == ctx.cust_names()[0]
    line = ctx.c_hi(f"{'Your' if own else subject + chr(39) + 's'} {service} is due",
                    f"{'Aapka' if own else subject + ' ka'} {service} due hai")
    if due:
        line += f" ({fmt_date(due)})"
    if last:
        line += ctx.c_hi(f" — last visit was {fmt_date(last)}", f" — pichli visit {fmt_date(last)} ko thi")
        if since and since > 30:
            line += f", about {round(since / 30)} months ago"
    core.append(line + ".")
    if slots:
        pref = _pref_slot_text(ctx)
        core.append(ctx.c_hi(f"We've kept {len(slots)} {pref + ' ' if pref else ''}slots for you.",
                             f"Aapke liye {len(slots)} {pref + ' ' if pref else ''}slots rakhe hain."))
    if offer:
        core.append(ctx.c_hi(f"Current offer: {offer}.", f"Abhi offer chal raha hai: {offer}."))
    cta_line, cta = _slot_cta(ctx, slots)
    accept = ctx.c_hi(f"Booked ✅ {slots[0] if slots else 'Your slot'} is confirmed at {ctx.name}. We'll send a reminder the day before.",
                      f"Booked ✅ {slots[0] if slots else 'Aapka slot'} {ctx.name} mein confirm hai. Ek din pehle reminder bhej denge.")
    return Draft(core=core, cta_line=cta_line, cta=cta, rationale=(
        "Customer recall due; service/dates/slots from trigger payload, only the merchant's live offer quoted; "
        f"language pref '{g(ctx.c, 'identity', 'language_pref')}' honoured; low-friction slot CTA; sent on merchant's behalf."),
        accept_body=accept, detail=" ".join(core), topic="your appointment", facts={"slots": slots})


def hc_appointment(ctx: Ctx) -> Draft:
    slot = ctx.p.get("slot_label") or ctx.p.get("time_label")
    appt = parse_dt(ctx.p.get("appointment_iso"))
    when = slot or (fmt_date(appt, weekday=True) + ", " + fmt_time(appt) if appt else None)
    service = humanize(ctx.p.get("service")) if ctx.p.get("service") else None
    core = [ctx.c_hi(
        f"Reminder: your {service + ' ' if service else ''}{_cust_service(ctx, 1)} is tomorrow{' — ' + when if when else ''}.",
        f"Reminder: aapka {service + ' ' if service else ''}{_cust_service(ctx, 1)} kal hai{' — ' + when if when else ''}.")]
    if ctx.locality:
        core.append(ctx.c_hi(f"We're at {ctx.locality}, {ctx.city}.", f"Hum {ctx.locality}, {ctx.city} mein hain."))
    cta_line = ctx.c_hi("Reply 1 to confirm or 2 to reschedule.", "Confirm ke liye 1, reschedule ke liye 2 reply kijiye.")
    accept = ctx.c_hi("Confirmed ✅ See you tomorrow!", "Confirmed ✅ Kal milte hain!")
    return Draft(core=core, cta_line=cta_line, cta="binary_confirm_reschedule", rationale=(
        "Appointment-tomorrow reminder; no time invented when payload lacks one; confirm/reschedule binary; language pref honoured."),
        accept_body=accept, detail=core[0], topic="tomorrow's appointment")


def hc_lapsed(ctx: Ctx) -> Draft:
    _, subject = ctx.cust_names()
    days = ctx.p.get("days_since_last_visit")
    last = parse_dt(g(ctx.c, "relationship", "last_visit"))
    if days is None:
        days = days_between(last, ctx.now)
    focus = ctx.p.get("previous_focus") or g(ctx.c, "preferences", "training_focus") or g(ctx.c, "preferences", "health_focus")
    months = ctx.p.get("previous_membership_months")
    visits = g(ctx.c, "relationship", "visits_total")
    offer = _cust_offer(ctx)
    core = []
    if days and days > 0:
        weeks = round(days / 7)
        span = f"about {weeks} weeks" if days < 90 else f"about {round(days / 30)} months"
        core.append(ctx.c_hi(f"It's been {span} since your last visit — happens to everyone, no judgment.",
                             f"Aapki last visit ko {span} ho gaye — koi baat nahi, sabke saath hota hai."))
    elif visits:
        core.append(ctx.c_hi(f"Thanks for your {visits} visits with us — we'd love to see you again.",
                             f"Aapki {visits} visits ke liye shukriya — aapko phir se dekhna accha lagega."))
    if focus and months:
        core.append(ctx.c_hi(f"You put in {months} solid months on {humanize(focus)} — easy to pick back up.",
                             f"Aapne {months} mahine {humanize(focus)} pe kaam kiya tha — wapas shuru karna easy hai."))
    elif focus:
        core.append(ctx.c_hi(f"Your {humanize(focus)} plan is still on file.", f"Aapka {humanize(focus)} plan hamare paas saved hai."))
    if offer:
        core.append(ctx.c_hi(f"Come back with: {offer}.", f"Wapas aane pe: {offer}."))
    cta_line = ctx.c_hi("Want us to hold a slot for you this week? Reply YES — no commitment.",
                        "Is hafte aapke liye slot hold kar dein? Reply YES — koi commitment nahi.")
    accept = ctx.c_hi(f"Done ✅ We'll message you the open slots this week{' — ' + offer + ' applies' if offer else ''}. See you soon!",
                      f"Done ✅ Is hafte ke open slots bhej rahe hain{' — ' + offer + ' lagu hoga' if offer else ''}. Jaldi milte hain!")
    return Draft(core=core, cta_line=cta_line, rationale=(
        "Customer lapse win-back; days since last visit from payload/relationship, no-shame tone, only the merchant's live offer; "
        "single no-commitment YES CTA; language pref honoured."), accept_body=accept, detail=" ".join(core), topic="your comeback slot")


def hc_refill(ctx: Ctx) -> Draft:
    mols = ctx.p.get("molecule_list") or []
    runout = parse_dt(ctx.p.get("stock_runs_out_iso"))
    saved = ctx.p.get("delivery_address_saved")
    offers = ctx.active_offers()
    senior = g(ctx.c, "identity", "senior_citizen")
    addressee, subject = ctx.cust_names()
    lang = ctx.cust_lang()
    core = []
    if ctx.slug == "pharmacies" and mols:
        who = f"{subject.replace('Mr. ', '')} ji ki" if senior and subject else ("aapki" if lang != "en" else "your")
        if lang != "en":
            line = f"{who.capitalize()} {len(mols)} regular medicines ({join_list(mols)})"
            line += f" {fmt_date(runout)} tak khatam ho jayengi." if runout else " refill due hain."
            core.append(line)
            core.append("Same dose, same pack ready hai.")
        else:
            line = f"{'Your' if not senior else subject + chr(39) + 's'} {len(mols)} regular medicines ({join_list(mols)})"
            line += f" run out on {fmt_date(runout)}." if runout else " are due for refill."
            core.append(line)
            core.append("Same dose, same pack is ready.")
        applicable = [o for o in offers if ("Senior" in o and senior) or "Delivery" in o]
        if applicable:
            core.append(("Applicable: " if lang == "en" else "Lagu offers: ") + "; ".join(applicable) + ".")
        if saved:
            core.append("Delivery to your saved address." if lang == "en" else "Saved address pe delivery ho jayegi.")
        cta_line = ctx.c_hi("Reply CONFIRM to dispatch, or tell us if the dosage has changed.",
                            "Dispatch ke liye CONFIRM reply kijiye, ya dosage badla ho toh bata dijiye.")
        accept = ctx.c_hi("Confirmed ✅ Packing now — we'll message when it's out for delivery.",
                          "Confirmed ✅ Pack ho raha hai — delivery nikalte hi message karenge.")
    else:
        core.append(ctx.c_hi(f"Time for your {_cust_service(ctx, 2)} with us.", f"Aapka {_cust_service(ctx, 2)} due hai."))
        if offers:
            core.append(ctx.c_hi(f"Current offer: {offers[0]}.", f"Abhi offer: {offers[0]}."))
        cta_line = ctx.c_hi("Reply YES and we'll share a convenient slot.", "Reply YES kijiye, hum convenient slot bhej denge.")
        accept = ctx.c_hi("Done ✅ We'll have it ready and message you the pickup/visit time.",
                          "Done ✅ Ready karke pickup/visit time message kar denge.")
    return Draft(core=core, cta_line=cta_line, cta="binary_confirm_cancel", rationale=(
        "Chronic refill due; molecules + run-out date from payload, only merchant's live offers (senior/delivery) applied; "
        "respectful tone for senior/family channel; single CONFIRM CTA."), accept_body=accept, detail=" ".join(core), topic="the refill")


def hc_trial_followup(ctx: Ctx) -> Draft:
    addressee, subject = ctx.cust_names()
    trial = parse_dt(ctx.p.get("trial_date"))
    slots = _slots(ctx, "next_session_options")
    offer = _cust_offer(ctx)
    core = []
    who = f"{subject}'s" if subject and subject != addressee else "your"
    core.append(ctx.c_hi(f"Hope you enjoyed {who} trial{' on ' + fmt_date(trial) if trial else ''}!",
                         f"Umeed hai {who.replace('your', 'aapka')} trial{' (' + fmt_date(trial) + ')' if trial else ''} accha laga!"))
    if slots:
        core.append(ctx.c_hi(f"Next session: {slots[0]}.", f"Agla session: {slots[0]}."))
    if offer:
        core.append(ctx.c_hi(f"To continue: {offer}.", f"Continue karne ke liye: {offer}."))
    cta_line, cta = _slot_cta(ctx, slots)
    accept = ctx.c_hi(f"Booked ✅ {slots[0] if slots else 'Your next session'} is reserved. See you there!",
                      f"Booked ✅ {slots[0] if slots else 'Agla session'} reserve ho gaya. Milte hain!")
    return Draft(core=core, cta_line=cta_line, cta=cta, rationale=(
        "Trial follow-up; trial date + next slot from payload; merchant's live offer only; single slot CTA."),
        accept_body=accept, detail=" ".join(core), topic="the next session")


def hc_wedding(ctx: Ctx) -> Draft:
    wd = parse_dt(ctx.p.get("wedding_date") or g(ctx.c, "preferences", "wedding_date"))
    dleft = ctx.p.get("days_to_wedding") or days_between(ctx.now, wd)
    trial = parse_dt(ctx.p.get("trial_completed"))
    step = humanize(ctx.p.get("next_step_window_open") or "next prep step")
    m30 = re.search(r"\s*(\d+)day$", step)
    if m30:
        step = f"{m30.group(1)}-day " + step[:m30.start()].strip()
    pref = _pref_slot_text(ctx)
    core = []
    if dleft and wd:
        core.append(ctx.c_hi(f"{dleft} days to your wedding on {fmt_date(wd)} 💍", f"Aapki shaadi ({fmt_date(wd)}) mein {dleft} din bache hain 💍"))
    if trial:
        core.append(ctx.c_hi(f"Since your bridal trial on {fmt_date(trial)}, the next step is the {step}.",
                             f"{fmt_date(trial)} ke bridal trial ke baad ab {step} ka time hai."))
    else:
        core.append(ctx.c_hi(f"Next step: the {step}.", f"Agla step: {step}."))
    cta_line = ctx.c_hi(f"Want us to block a {pref + ' ' if pref else ''}slot next week to start? Reply YES.",
                        f"Shuru karne ke liye agle hafte {pref + ' ka ' if pref else 'ek '}slot block kar dein? Reply YES.")
    accept = ctx.c_hi(f"Done ✅ We'll send your {pref + ' ' if pref else ''}slot options for next week right here.",
                      f"Done ✅ Agle hafte ke {pref + ' ' if pref else ''}slot options yahin bhej rahe hain.")
    return Draft(core=core, cta_line=cta_line, rationale=(
        "Bridal follow-up; wedding date, days-to-go and trial date from payload/customer; preferred slot honoured; "
        "no invented package price; single YES CTA."), accept_body=accept, detail=" ".join(core), topic="your bridal prep")


def hc_generic(ctx: Ctx) -> Draft:
    offer = _cust_offer(ctx)
    kind = humanize(ctx.t.get("kind") or "update")
    core = [ctx.c_hi(f"A quick {kind} note from us.", f"Aapke liye ek chhota sa {kind} update.")]
    if offer:
        core.append(ctx.c_hi(f"Current offer: {offer}.", f"Abhi offer: {offer}."))
    cta_line = ctx.c_hi("Reply YES and we'll share the details.", "Reply YES kijiye, details bhej denge.")
    return Draft(core=core, cta_line=cta_line, rationale=f"Customer-scoped '{kind}' trigger; merchant's live offer only; single YES CTA.",
                 accept_body=ctx.c_hi("Great ✅ Sharing the details here shortly.", "Great ✅ Details yahin bhej rahe hain."),
                 detail=" ".join(core), topic=kind)


def h_customer_without_context(ctx: Ctx) -> Draft:
    """Customer-scoped trigger but CustomerContext missing: ask merchant (no fabrication)."""
    cid = ctx.t.get("customer_id") or "a customer"
    kind = humanize(ctx.t.get("kind") or "follow-up")
    core = [f"a {kind} is due for one of your {ctx.words['people']} ({cid}), but I don't have their details yet."]
    cta_line = ctx.hi("Want me to draft the reminder for you to send? Reply YES.", "Reminder draft kar doon, aap bhej dena? Reply YES.")
    return Draft(core=core, cta_line=cta_line, rationale="Customer trigger without CustomerContext — routed to merchant instead of guessing.",
                 accept_body="Drafting it now — you'll get a copy-paste reminder here. Reply CONFIRM once sent.", topic=kind)


MERCHANT_HANDLERS = {
    "research_digest": h_digest, "research_digest_release": h_digest, "category_research_digest_release": h_digest,
    "regulation_change": h_digest, "compliance_alert": h_digest, "supply_alert": h_digest,
    "cde_opportunity": h_digest, "category_trend_movement": h_digest,
    "perf_dip": h_perf, "perf_spike": h_perf, "seasonal_perf_dip": h_seasonal_dip,
    "renewal_due": h_renewal, "winback_eligible": h_winback_merchant, "dormant_with_vera": h_dormant,
    "curious_ask_due": h_curious, "scheduled_recurring": h_curious,
    "festival_upcoming": h_festival, "ipl_match_today": h_ipl, "review_theme_emerged": h_review_theme,
    "milestone_reached": h_milestone, "active_planning_intent": h_planning, "gbp_unverified": h_gbp_unverified,
    "profile_incomplete": h_gbp_unverified, "competitor_opened": h_competitor, "category_seasonal": h_category_seasonal,
}

CUSTOMER_HANDLERS = {
    "recall_due": hc_recall, "appointment_tomorrow": hc_appointment,
    "customer_lapsed_soft": hc_lapsed, "customer_lapsed_hard": hc_lapsed, "winback": hc_lapsed,
    "chronic_refill_due": hc_refill, "trial_followup": hc_trial_followup,
    "wedding_package_followup": hc_wedding, "bridal_followup": hc_wedding,
}


def _scrub(text: str, taboos: List[str]) -> str:
    text = URL_RE.sub("", text)
    for t in taboos or []:
        t = re.sub(r"\s*\(.*?\)", "", str(t)).strip()
        if t:
            text = re.sub(re.escape(t), "", text, flags=re.I)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r" +([.,;:])", r"\1", text)
    return text.strip()


def _cap_first(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


def compose_full(category: dict, merchant: dict, trigger: dict,
                 customer: Optional[dict] = None, now: Optional[str] = None) -> Dict[str, Any]:
    """compose() plus internal fields (accept_body, detail, topic) used by the conversation engine."""
    ctx = Ctx(category, merchant, trigger, customer, now)
    kind = (trigger or {}).get("kind") or ""
    customer_scope = (trigger or {}).get("scope") == "customer" or kind in CUSTOMER_HANDLERS

    if customer_scope and customer:
        draft = CUSTOMER_HANDLERS.get(kind, hc_generic)(ctx)
        send_as = "merchant_on_behalf"
        opening = draft.opening or _cust_open(ctx)
        body = f"{opening} {' '.join(draft.core)} {draft.cta_line}"
        params = [opening, " ".join(draft.core), draft.cta_line]
        template = f"merchant_{kind or 'generic'}_v1"
    else:
        draft = (h_customer_without_context if customer_scope else MERCHANT_HANDLERS.get(kind, h_generic))(ctx)
        send_as = "vera"
        if draft.opening:
            opening = draft.opening
            if opening.endswith(("!", ".", ":")) and draft.core and not draft.core[0].startswith(("\n", " ")):
                draft.core[0] = _cap_first(draft.core[0])
            core_txt = "".join(draft.core) if draft.core and draft.core[0].startswith(("\n", " ")) else " " + " ".join(draft.core)
            body = f"{opening}{core_txt} {draft.cta_line}" if not core_txt.endswith("\n") else f"{opening}{core_txt}{draft.cta_line}"
            params = [opening, core_txt.strip(), draft.cta_line]
        else:
            opening = f"{ctx.sal},"
            core = list(draft.core)
            body = f"{opening} {' '.join(core)} {draft.cta_line}"
            params = [ctx.sal, _cap_first(" ".join(core)), draft.cta_line]
        template = f"vera_{kind or 'generic'}_v1"

    taboos = g(ctx.cat, "voice", "vocab_taboo", default=[]) or []
    body = _scrub(body, taboos)
    body = re.sub(r"\n +", "\n", body)
    return {
        "body": body,
        "cta": draft.cta,
        "send_as": send_as,
        "suppression_key": (trigger or {}).get("suppression_key") or f"{kind}:{(merchant or {}).get('merchant_id')}",
        "rationale": draft.rationale,
        "template_name": template,
        "template_params": [_scrub(p, taboos) for p in params],
        "_accept_body": _scrub(draft.accept_body, taboos),
        "_detail": _scrub(draft.detail, taboos),
        "_topic": draft.topic,
        "_hinglish": ctx.hinglish if send_as == "vera" else ctx.cust_lang() != "en",
    }


def compose(category: dict, merchant: dict, trigger: dict,
            customer: Optional[dict] = None, now: Optional[str] = None) -> Dict[str, Any]:
    """Public contract from the challenge brief: body, cta, send_as, suppression_key, rationale."""
    full = compose_full(category, merchant, trigger, customer, now)
    return {k: full[k] for k in ("body", "cta", "send_as", "suppression_key", "rationale",
                                 "template_name", "template_params")}
