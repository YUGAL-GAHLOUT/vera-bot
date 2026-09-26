# Vera challenge bot — deterministic 4-context composer

**Approach.** No LLM: every message comes from `compose(category, merchant, trigger, customer?)` in `composer.py`, which routes on `trigger.kind` to ~25 per-kind handlers (research/compliance/alert/CDE digests, perf dip/spike, seasonal dip, renewal, win-back, dormant, curious-ask, festival, IPL, review themes, milestones, planning intent, GBP verification, competitor, seasonal demand, and customer recall/appointment/lapse/refill/trial/bridal). Each handler uses only facts found in the four contexts: payload numbers, digest titles with their source, the merchant's own 30-day numbers against `peer_stats`, live offers, `customer_aggregate` cohorts, review quotes and slot labels. Every message has one CTA.

About 75% of triggers have placeholder payloads. For those, the handler falls back to the merchant's numbers, the category's trend signals, seasonal beats and catalog offers, and the peer benchmarks. It never makes up competitor names, prices, times or citations. Taboo vocabulary and URLs are removed after composition. Merchants whose `languages` include `hi` get a Hinglish CTA. Customer messages follow `language_pref` (hi / hi-en mix / en, plus a regional greeting for ta, te and kn).

**Conversation engine** (`conversation_handlers.py`). The engine is rule-based and handles these cases, checked in this order:
1. Opt-out: the bot ends the conversation and stops messaging that merchant.
2. Auto-reply (canned phrasing, or the same long text repeated, also tracked across conversations for one merchant): one nudge aimed at the owner, then wait 24h, then end.
3. Hostile message: one apology with a way to opt out, then end.
4. Off-topic request (GST and similar): a polite decline, then back to the original topic.
5. "Not now": wait (30 min to 24h).
6. A price question: answered only with figures present in the context.
7. Intent ("yes", "let's do it", "judna hai"): the bot sends the drafted work straight away, then confirms, then closes. It never asks another qualifying question.

The language is detected on every turn. The engine never sends the same text twice in one conversation, and it caps itself at 6 bot turns.

**Server** (`bot.py`, FastAPI). It implements `/v1/context` (versioned; 409 on a stale version, 400 on bad input), `/v1/tick`, `/v1/reply`, `/v1/healthz`, `/v1/metadata`, and the optional `/v1/teardown`. Tick rules:
- Highest urgency goes first, with at most 20 actions per tick.
- A trigger's suppression key is never reused.
- Each merchant or customer gets at most one message per tick; extra triggers are held in a queue and sent on the next tick.
- The same merchant or customer never gets two messages of the same kind.
- Customers with no consent on record are never messaged.
- A customer trigger that arrives before its customer context waits until the context arrives.

## Run

```bash
pip install -r requirements.txt
uvicorn bot:app --host 0.0.0.0 --port 8080      # single worker: state is in memory
cd dataset && python generate_dataset.py --out ./expanded && cd ..
python generate_submission.py                     # -> submission.jsonl (30 test pairs)
pip install -r requirements-dev.txt && pytest -q  # 21 tests
```

Deploy with `render.yaml` / `Dockerfile` / `Procfile`. Use an always-on instance: a restart wipes the in-memory context. Metadata comes from the `TEAM_NAME`, `TEAM_MEMBERS` and `CONTACT_EMAIL` env vars.

**Tradeoffs.** Templates are deterministic, fast (a few ms) and cannot hallucinate, but they are less fluid than an LLM and weaker on unusual free-text replies. Hinglish is template-level, not generated.

**Context that would have helped most:** real open appointment slots for every merchant, a list of each merchant's live services with prices, review counts and ratings, and non-placeholder payloads for the generated triggers.
