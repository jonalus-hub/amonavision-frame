"""
Scoring engine for the AmonaVision slideshow.

Each session gets a priority score; auto-advance draws from a shuffled
"ticket bag" weighted by that score — not sequentially, not always the
single highest scorer, and not an independent random draw every cycle
either (see build_round()'s docstring for why: both of those were tried
and both produced a visibly repetitive slideshow).

Score components (each normalised to [0, 1] before weighting):
  novelty          — brand-new photos surface quickly; decays with age
  anti_repetition  — suppresses recently-shown sessions; lifts over time
  sender_balance   — boosts senders under-represented in recent history
  explicit         — sessions that received a voice reply score higher
  implicit         — sessions with a high average dwell PER VIEWING or a
                     high revisit RATE score higher (not raw lifetime totals,
                     which would just reward being old)

All weights and parameters are read from config.txt [Scoring] and forwarded
here as a plain dict — this module has no I/O of its own.
"""

import math
import random
import time
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Individual component functions
# ---------------------------------------------------------------------------

def _novelty(session: Dict, now_ts: float, halflife_days: float) -> float:
    days_old = max(0.0, (now_ts - session.get('created_ts', now_ts)) / 86400.0)
    return math.pow(0.5, days_old / max(halflife_days, 0.01))


def _anti_repetition(session: Dict, now_ts: float, suppression_hours: float) -> float:
    last_shown = session.get('last_shown_at')
    if last_shown is None:
        return 1.0
    hours_ago = max(0.0, (now_ts - last_shown) / 3600.0)
    return 1.0 - math.exp(-hours_ago / max(suppression_hours, 0.01))


def _sender_balance(session: Dict, recent_senders: List, window: int) -> float:
    if not recent_senders:
        return 1.0
    sample = recent_senders[:window]
    count = sample.count(session.get('sender_id'))
    return 1.0 - (count / len(sample))


def _explicit_interest(session: Dict, now_ts: float, halflife_days: float) -> float:
    replies = session.get('voice_reply_count', 0)
    last_reply_ts = session.get('last_voice_reply_at')
    if not replies or not last_reply_ts:
        return 0.0
    days_since = max(0.0, (now_ts - last_reply_ts) / 86400.0)
    base = min(replies / 2.0, 1.0)
    return base * math.pow(0.5, days_since / max(halflife_days, 0.01))


def _implicit_interest(session: Dict, threshold_seconds: float) -> float:
    # total_dwell_seconds / revisit_count are LIFETIME sums that never reset,
    # so comparing them straight against a per-viewing threshold saturates
    # this to its max after just ~2 showings — a session shown twice would
    # then score identically to one shown 30,000 times, permanently
    # outranking anything freshly arrived (which starts at 0). Normalise by
    # show_count instead: average dwell per viewing, and how often a viewing
    # of this session turned into a manual revisit — both genuine "does
    # someone actually linger on this" signals regardless of age.
    shows = max(session.get('show_count') or 0, 1)
    avg_dwell = (session.get('total_dwell_seconds') or 0.0) / shows
    revisit_rate = (session.get('revisit_count') or 0) / shows

    dwell_part = min(avg_dwell / max(threshold_seconds, 1.0), 1.0) * 0.7
    # Revisiting ~1 in 7 times you land on it ("you keep coming back to this
    # one") counts as maximal revisit interest.
    revisit_part = min(revisit_rate / 0.15, 1.0) * 0.3
    return min(dwell_part + revisit_part, 1.0)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def score_breakdown(
    session: Dict[str, Any],
    now_ts: float,
    recent_senders: List,
    settings: Dict[str, Any],
) -> Dict[str, float]:
    """Every component score (raw, unweighted, 0-1) plus the final weighted
    'total' — the same numbers score_session() sums, broken out so a caller
    can show *why* a session ranks where it does (see web/app.py's /db
    scoring table) instead of just the opaque total."""
    w_nov = settings.get('weight_novelty', 1.0)
    w_rep = settings.get('weight_anti_repetition', 1.0)
    w_bal = settings.get('weight_sender_balance', 1.0)
    w_exp = settings.get('weight_explicit_interest', 1.0)
    w_imp = settings.get('weight_implicit_interest', 1.0)

    novelty = _novelty(session, now_ts, settings.get('novelty_halflife_days', 3.0))
    anti_repetition = _anti_repetition(session, now_ts, settings.get('suppression_hours', 24.0))
    sender_balance = _sender_balance(session, recent_senders, settings.get('sender_balance_window', 20))
    explicit = _explicit_interest(session, now_ts, settings.get('explicit_halflife_days', 30.0))
    implicit = _implicit_interest(session, settings.get('implicit_dwell_threshold_seconds', 30.0))

    total = 0.0
    if w_nov > 0:
        total += novelty * w_nov
    if w_rep > 0:
        total += anti_repetition * w_rep
    if w_bal > 0:
        total += sender_balance * w_bal
    if w_exp > 0:
        total += explicit * w_exp
    if w_imp > 0:
        total += implicit * w_imp

    return {
        'novelty': novelty,
        'anti_repetition': anti_repetition,
        'sender_balance': sender_balance,
        'explicit': explicit,
        'implicit': implicit,
        'total': total,
    }


def score_session(
    session: Dict[str, Any],
    now_ts: float,
    recent_senders: List,
    settings: Dict[str, Any],
) -> float:
    """Return the total weighted priority score for one session."""
    return score_breakdown(session, now_ts, recent_senders, settings)['total']


def selection_weights(
    sessions: List[Dict[str, Any]],
    now_ts: float,
    recent_senders: List,
    settings: Dict[str, Any],
) -> List[float]:
    """Each session's raw score, floored at 0 — the weight build_round() turns
    into tickets. Exposed so a caller (the /db scoring table) can show the
    same numbers build_round() actually uses, instead of a second,
    potentially-drifted copy of this math."""
    return [max(score_session(s, now_ts, recent_senders, settings), 0.0) for s in sessions]


# How many times the single highest-ticket session can appear in one round
# before every other eligible session is guaranteed to have had a turn.
# Keeps one very high-scoring session from dominating a round even if its
# score is many times everyone else's.
MAX_TICKETS_PER_SESSION = 6


def tickets_for(weights: List[float]) -> List[int]:
    """Convert raw scores to integer ticket counts: at least 1 each (nothing
    is ever excluded), proportional to score relative to the lowest-scoring
    eligible session, capped at MAX_TICKETS_PER_SESSION. Shared by
    build_round() and the /db dashboard, so the ticket bag it displays is the
    one actually used, not a redrawn approximation."""
    positive = [w for w in weights if w > 0]
    floor = min(positive) if positive else 1.0
    return [max(1, min(round(w / floor), MAX_TICKETS_PER_SESSION)) for w in weights]


def build_round(
    sessions: List[Dict[str, Any]],
    current_id: Optional[int],
    recent_senders: List,
    settings: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Build one shuffled "round" — a ticket bag, not a single independent
    draw. Each eligible session gets a number of tickets proportional to its
    score (at least 1, so nothing is ever excluded and nothing can hog more
    than MAX_TICKETS_PER_SESSION), the bag is shuffled, and the caller pops
    one session off per auto-advance. Once empty, call this again to build
    the next round with fresh scores.

    Two earlier designs were tried and both failed in practice, confirmed
    live on the real frame:
    - A strict max() (score_session()'s original caller) always shows
      whichever single session currently scores highest. Scores shift cycle
      to cycle — anti_repetition drops right after a session is shown, then
      climbs back — so two sessions ended up perpetually trading the #1
      spot: 13 and 12 showings in 28s while three others sat untouched.
    - Independent weighted-random draws (random.choices every cycle) fixed
      that, but are "clumpy" by nature: with live weights this close
      (17-23% each), pure independent sampling still produces visible
      streaks of the same session by chance alone — the same reason music
      players had to stop using literally-random shuffle. A shuffled ticket
      bag caps how many times a session can appear before every other
      eligible session has had at least one turn.
    """
    if not sessions:
        return []

    now_ts = time.time()
    candidates = [s for s in sessions if s['id'] != current_id] or sessions

    weights = selection_weights(candidates, now_ts, recent_senders, settings)
    tickets = tickets_for(weights)

    bag: List[Dict[str, Any]] = []
    for s, n in zip(candidates, tickets):
        bag.extend([s] * n)

    random.shuffle(bag)
    return bag
