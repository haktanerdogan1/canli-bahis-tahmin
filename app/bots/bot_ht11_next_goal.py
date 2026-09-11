"""HT 1-1 next-goal experiment. No fitted probability, no production vote.

Each checkpoint is a separate strategy, not four bets on the same match.
This module deliberately has no database/network side effects.
"""
from datetime import datetime, timezone

CHECKPOINTS = (60, 65, 70, 75)
VERSION = "ht11-shadow-v1"


def timestamp(value):
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return (dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt).timestamp()
    except (TypeError, ValueError):
        return None


def score(value):
    return value if type(value) is int and value >= 0 else None


class HT11NextGoalBot:
    name = "bot_ht11_next_goal"
    version = VERSION
    mode = "shadow"

    def evaluate(self, match, halftime, now):
        if match.get("status") != "LIVE" or not halftime:
            return None
        if halftime["source_match_id"] != match.get("source_match_id"):
            return None
        if (halftime["home_score"], halftime["away_score"]) != (1, 1):
            return None
        seen = timestamp(match.get("last_seen_at"))
        confirmed = timestamp(halftime["observed_at"])
        if seen is None or confirmed is None or not 0 <= now - seen <= 120:
            return None
        if not 0 < seen - confirmed <= 3 * 3600:
            return None
        minute = match.get("minute")
        if type(minute) is not int:
            return None
        checkpoint = next((m for m in CHECKPOINTS if m <= minute <= m + 1), None)
        home, away = score(match.get("home_score")), score(match.get("away_score"))
        if checkpoint is None or home is None or away is None or min(home, away) < 1:
            return None
        total = home + away
        return dict(match_id=match["id"], source_match_id=match["source_match_id"],
                    checkpoint=checkpoint, minute=minute, home_score=home,
                    away_score=away, initial_goals=total, goal_line=total + .5,
                    market=f"Maç Sonu {total + .5:.1f} Üst",
                    observed_at=match["last_seen_at"], bot_version=self.version,
                    probability=None, mode=self.mode)
