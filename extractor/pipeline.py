"""Extraction pipeline: rule P-level for everything, LLM semantics for
text-bearing sources under a per-source cost policy.
"""
import json
import logging
import time
from datetime import datetime

from extractor import llm, rules
from storage import Extraction, RawSignal

log = logging.getLogger(__name__)

# LLM policy per source: "always" | "low_rated" (appstore <=3 stars) | "never"
LLM_POLICY = {
    "sam_gov": "never",        # title only, description is a link
    "usaspending": "never",    # title only, huge volume
    "cfpb": "never",           # structured title only, narratives discontinued
    "appstore_charts": "never",  # ranking rows, no text
    "appstore": "low_rated",   # cost cap: only reviews rated <=3
    "reddit_rss": "always",
    "reddit": "always",
    "freelancer": "always",
    "manual": "always",
}


def _needs_llm(signal):
    policy = LLM_POLICY.get(signal.source, "never")
    if policy == "always":
        return bool(signal.title or signal.description)
    if policy == "low_rated":
        return signal.rating is not None and signal.rating <= 3
    return False


def candidates(session, source=None, limit=None, force=False):
    q = session.query(RawSignal)
    if source:
        q = q.filter(RawSignal.source == source)
    if not force:
        done = session.query(Extraction.signal_id).subquery()
        q = q.filter(~RawSignal.id.in_(done))
    q = q.order_by(RawSignal.id)
    if limit:
        q = q.limit(limit)
    return q.all()


def extract_batch(session, source=None, limit=None, force=False, dry_run=False, use_llm=True):
    stats = {"total": 0, "rule_p": 0, "llm_ok": 0, "llm_fail": 0, "no_p": 0,
             "prompt_tokens": 0, "completion_tokens": 0}
    signals = candidates(session, source, limit, force)
    stats["total"] = len(signals)

    for sig in signals:
        text = " ".join(filter(None, [sig.title, sig.description]))
        rec = {
            "signal_id": sig.id,
            "p_level": rules.rule_p_level(sig),
            "p_method": "rule" if rules.rule_p_level(sig) else "none",
            "trigger_type": rules.detect_trigger(text) if sig.source == "news" else None,
            "cost_hint": rules.rule_cost_hint(sig),
            "audience": None, "scenario": None, "pain_point": None,
            "urgency": None, "current_solution": None, "alternatives": None,
            "supply_gap": None, "evidence_json": None, "model": None,
            "prompt_tokens": 0, "completion_tokens": 0, "llm_raw": None,
            "extracted_at": datetime.utcnow(),
        }
        if rec["p_level"]:
            stats["rule_p"] += 1
        else:
            stats["no_p"] += 1

        if use_llm and _needs_llm(sig) and not dry_run:
            try:
                fields, evidence, model, ptok, ctok, raw = llm.extract_semantics(sig)
                rec.update({
                    "pain_point": fields["pain_point"] or None,
                    "audience": fields["audience"] or None,
                    "scenario": fields["scenario"] or None,
                    "urgency": fields["urgency"] or None,
                    "current_solution": fields["current_solution"] or None,
                    "alternatives": fields["alternatives"] or None,
                    "supply_gap": fields["supply_gap"] or None,
                    "evidence_json": json.dumps(evidence, ensure_ascii=False),
                    "model": model,
                    "prompt_tokens": ptok,
                    "completion_tokens": ctok,
                    "llm_raw": raw,
                })
                stats["llm_ok"] += 1
                stats["prompt_tokens"] += ptok
                stats["completion_tokens"] += ctok
                time.sleep(0.2)
            except llm.LLMNotConfigured:
                log.warning("OPENAI_API_KEY not set; LLM extraction skipped")
                break
            except Exception as e:
                log.warning("LLM failed for signal %d: %s", sig.id, e)
                stats["llm_fail"] += 1

        if dry_run:
            log.info("[dry-run] #%d %s/%s -> P%s (%s) cost_hint=%s",
                     sig.id, sig.source, sig.signal_type, rec["p_level"], rec["p_method"], rec["cost_hint"])
            continue

        session.merge(Extraction(**rec))
        if stats["llm_ok"] % 100 == 0:
            session.commit()
    if not dry_run:
        session.commit()

    cost = llm.estimate_cost(llm.MODEL, stats["prompt_tokens"], stats["completion_tokens"])
    stats["estimated_cost_usd"] = round(cost, 4) if cost is not None else None
    return stats
