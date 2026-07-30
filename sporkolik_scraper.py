"""Async scraper for sporkolik.net live scores + per-match statistics.

The live-score page (https://www.sporkolik.net/canli-skor) is server rendered:
each match is a ``div.live-scores-match.match-card`` carrying data-* attributes.
Detailed statistics are NOT in the HTML; the match page loads them through the
WordPress admin-ajax endpoint, which we call directly:

    POST /wp-admin/admin-ajax.php
        action=get_match_statistics&match_id=<id>&match_status=<status>&_lang=tr

Usage:
    python sporkolik_scraper.py                 # pretty print live matches + stats
    python sporkolik_scraper.py --json out.json # dump everything to JSON

    from sporkolik_scraper import SporkolikScraper
    async with SporkolikScraper() as s:
        matches = await s.scrape_live(with_stats=True)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable

import aiohttp
from bs4 import BeautifulSoup

BASE_URL = "https://www.sporkolik.net"
LIVE_URL = f"{BASE_URL}/canli-skor"
AJAX_URL = f"{BASE_URL}/wp-admin/admin-ajax.php"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

# Live statuses as exposed by data-status on the match cards.
LIVE_STATUSES = {"inprogress", "halftime", "delayed", "suspended"}

# Stat keys we care about most; used by MatchStats.summary().
KEY_STATS = (
    "ballPossession",
    "totalShotsOnGoal",
    "shotsOnGoal",
    "shotsOffGoal",
    "blockedScoringAttempt",
    "bigChanceCreated",
    "bigChanceMissed",
    "cornerKicks",
    "fouls",
    "yellowCards",
    "redCards",
    "offsides",
    "goalkeeperSaves",
    "totalShotsInsideBox",
    "totalShotsOutsideBox",
    "expectedGoals",
)

log = logging.getLogger("sporkolik")


# --------------------------------------------------------------------------- #
# Data models
# --------------------------------------------------------------------------- #
@dataclass
class StatItem:
    key: str
    name: str
    group: str
    home: str
    away: str
    home_value: float | None = None
    away_value: float | None = None


@dataclass
class MatchStats:
    """Statistics for one period ("ALL", "1ST", "2ND")."""

    period: str
    items: list[StatItem] = field(default_factory=list)

    def as_dict(self) -> dict[str, dict[str, Any]]:
        return {
            i.key: {
                "name": i.name,
                "group": i.group,
                "home": i.home,
                "away": i.away,
                "home_value": i.home_value,
                "away_value": i.away_value,
            }
            for i in self.items
        }

    def get(self, key: str) -> StatItem | None:
        return next((i for i in self.items if i.key == key), None)

    def summary(self) -> dict[str, tuple[str, str]]:
        d = self.as_dict()
        return {k: (d[k]["home"], d[k]["away"]) for k in KEY_STATS if k in d}


@dataclass
class Match:
    match_id: str
    home: str
    away: str
    home_score: int | None
    away_score: int | None
    status: str
    status_description: str
    minute: str | None
    start_time: str | None
    tournament: str | None
    tournament_id: str | None
    season_id: str | None
    home_team_id: str | None
    away_team_id: str | None
    home_red_cards: int = 0
    away_red_cards: int = 0
    url: str | None = None
    has_statistics: bool = False
    stats: dict[str, MatchStats] = field(default_factory=dict)

    @property
    def is_live(self) -> bool:
        return self.status in LIVE_STATUSES

    @property
    def all_stats(self) -> MatchStats | None:
        return self.stats.get("ALL")

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["stats"] = {p: s.as_dict() for p, s in self.stats.items()}
        return d

    def __str__(self) -> str:
        minute = f"{self.minute:>4}" if self.minute else f"{self.status_description:>4}"
        return (
            f"[{minute}] {self.home} {self.home_score}-{self.away_score} {self.away}"
            f"  ({self.tournament or '?'})"
        )


# --------------------------------------------------------------------------- #
# Scraper
# --------------------------------------------------------------------------- #
class SporkolikScraper:
    def __init__(
        self,
        concurrency: int = 6,
        timeout: float = 25.0,
        retries: int = 2,
        lang: str = "tr",
    ) -> None:
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._sem = asyncio.Semaphore(concurrency)
        self._retries = retries
        self._lang = lang
        self._session: aiohttp.ClientSession | None = None

    # -- lifecycle ---------------------------------------------------------- #
    async def __aenter__(self) -> "SporkolikScraper":
        self._session = aiohttp.ClientSession(
            timeout=self._timeout,
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "tr,en;q=0.8",
            },
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError("Use 'async with SporkolikScraper() as s: ...'")
        return self._session

    # -- low level ---------------------------------------------------------- #
    async def _request(self, method: str, url: str, **kw: Any) -> aiohttp.ClientResponse:
        last: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                async with self._sem:
                    resp = await self.session.request(method, url, **kw)
                    async with resp:
                        resp.raise_for_status()
                        resp._body = await resp.read()  # noqa: SLF001 - cache body
                        return resp
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last = exc
                if attempt < self._retries:
                    await asyncio.sleep(0.6 * (attempt + 1))
        raise RuntimeError(f"{method} {url} failed after retries") from last

    async def _get_text(self, url: str) -> str:
        resp = await self._request("GET", url, allow_redirects=True)
        return resp._body.decode("utf-8", "replace")  # noqa: SLF001

    async def _ajax(self, action: str, **params: Any) -> Any:
        """POST to admin-ajax.php and return the ``data`` payload (or None)."""
        form = aiohttp.FormData()
        form.add_field("action", action)
        form.add_field("_lang", self._lang)
        for k, v in params.items():
            if v is not None:
                form.add_field(k, str(v))
        try:
            resp = await self._request(
                "POST",
                AJAX_URL,
                data=form,
                headers={"X-Requested-With": "XMLHttpRequest", "Referer": LIVE_URL},
            )
        except RuntimeError:
            log.warning("ajax %s failed", action)
            return None
        try:
            payload = json.loads(resp._body.decode("utf-8", "replace"))  # noqa: SLF001
        except json.JSONDecodeError:
            log.warning("ajax %s returned non-JSON", action)
            return None
        if not payload.get("success"):
            return None
        return payload.get("data")

    # -- parsing ------------------------------------------------------------ #
    @staticmethod
    def _text(node: Any) -> str | None:
        if node is None:
            return None
        t = node.get_text(" ", strip=True)
        return t or None

    @staticmethod
    def _int(value: Any) -> int | None:
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return None

    def parse_matches(self, html: str) -> list[Match]:
        soup = BeautifulSoup(html, "html.parser")
        matches: list[Match] = []

        for card in soup.select("div.live-scores-match.match-card"):
            home_box = card.select_one(".live-scores-team.home")
            away_box = card.select_one(".live-scores-team.away")
            if not home_box or not away_box:
                continue

            link = card.select_one("a.match-card__link")
            url = link.get("href") if link else None

            # Tournament: nearest preceding league header, else aria-label fallback.
            tournament = None
            header = card.find_previous(class_="live-scores-tournament-title")
            if header:
                tournament = self._text(header)
            if not tournament:
                aria = card.get("aria-label") or ""
                parts = [p.strip() for p in aria.split(";") if p.strip()]
                if len(parts) >= 2:
                    tournament = parts[1].split(",")[0].strip() or None

            matches.append(
                Match(
                    match_id=str(card.get("data-match-id") or ""),
                    home=self._text(home_box.select_one(".live-scores-name")) or "?",
                    away=self._text(away_box.select_one(".live-scores-name")) or "?",
                    home_score=self._int(self._text(home_box.select_one(".live-scores-score"))),
                    away_score=self._int(self._text(away_box.select_one(".live-scores-score"))),
                    status=(card.get("data-status") or "").lower(),
                    status_description=card.get("data-status-description") or "",
                    minute=self._text(card.select_one(".live-scores-minute")),
                    start_time=self._text(card.select_one(".live-scores-time")),
                    tournament=tournament,
                    tournament_id=card.get("data-tournament-id"),
                    season_id=card.get("data-season-id"),
                    home_team_id=card.get("data-home-team-id"),
                    away_team_id=card.get("data-away-team-id"),
                    home_red_cards=self._int(card.get("data-home-red-cards")) or 0,
                    away_red_cards=self._int(card.get("data-away-red-cards")) or 0,
                    url=url,
                    has_statistics=(card.get("data-has-statistics") == "true"),
                )
            )
        return matches

    @staticmethod
    def parse_statistics(data: Any) -> dict[str, MatchStats]:
        """Convert the get_match_statistics payload into period -> MatchStats."""
        out: dict[str, MatchStats] = {}
        if not isinstance(data, dict):
            return out
        for period in data.get("statistics") or []:
            name = period.get("period") or "ALL"
            stats = MatchStats(period=name)
            for group in period.get("groups") or []:
                gname = group.get("groupName") or ""
                for item in group.get("statisticsItems") or []:
                    key = item.get("key") or item.get("name") or ""
                    stats.items.append(
                        StatItem(
                            key=key,
                            name=item.get("name") or key,
                            group=gname,
                            home=str(item.get("home", "")),
                            away=str(item.get("away", "")),
                            home_value=item.get("homeValue"),
                            away_value=item.get("awayValue"),
                        )
                    )
            out[name] = stats
        return out

    # -- public API --------------------------------------------------------- #
    async def fetch_live_page(self) -> str:
        return await self._get_text(LIVE_URL)

    async def get_matches(self, live_only: bool = True) -> list[Match]:
        matches = self.parse_matches(await self.fetch_live_page())
        return [m for m in matches if m.is_live] if live_only else matches

    async def fetch_statistics(self, match: Match) -> dict[str, MatchStats]:
        data = await self._ajax(
            "get_match_statistics",
            match_id=match.match_id,
            match_status=match.status or "inprogress",
        )
        stats = self.parse_statistics(data)
        match.stats = stats
        match.has_statistics = bool(stats)
        return stats

    async def fetch_details(self, match: Match) -> dict[str, Any] | None:
        """Extra metadata (managers, venue, aggregate score, customId...)."""
        data = await self._ajax("get_match_details", match_id=match.match_id)
        return data if isinstance(data, dict) else None

    async def fetch_all_statistics(self, matches: Iterable[Match]) -> None:
        await asyncio.gather(*(self.fetch_statistics(m) for m in matches))

    async def scrape_live(self, with_stats: bool = True) -> list[Match]:
        matches = await self.get_matches(live_only=True)
        if with_stats and matches:
            await self.fetch_all_statistics(matches)
        return matches


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _print(matches: list[Match]) -> None:
    if not matches:
        print("Şu anda canlı maç yok.")
        return
    for m in matches:
        print(m)
        stats = m.all_stats
        if not stats:
            print("      (istatistik yok)")
            continue
        for key, (h, a) in stats.summary().items():
            label = stats.get(key).name if stats.get(key) else key
            print(f"      {label:<26} {h:>6}  -  {a:<6}")
        print()


async def _main() -> None:
    ap = argparse.ArgumentParser(description="Sporkolik canlı skor scraper")
    ap.add_argument("--json", metavar="PATH", help="sonucu JSON dosyasına yaz")
    ap.add_argument("--all", action="store_true", help="canlı olmayan maçları da al")
    ap.add_argument("--no-stats", action="store_true", help="istatistikleri çekme")
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    async with SporkolikScraper(concurrency=args.concurrency) as s:
        matches = await s.get_matches(live_only=not args.all)
        if not args.no_stats and matches:
            await s.fetch_all_statistics(matches)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump([m.to_dict() for m in matches], fh, ensure_ascii=False, indent=2)
        print(f"{len(matches)} maç -> {args.json}")
    else:
        _print(matches)


if __name__ == "__main__":
    asyncio.run(_main())
