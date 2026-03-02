#!/usr/bin/env python3
"""
Polymarket BTC 15-Minute Up/Down Trading Bot

Auto-discovers BTC 15m binary markets via the Gamma API, enters positions
when the favored side's order book confidence is >= 0.91, monitors for
stop-loss, and logs all trades. Supports paper trading (--dry-run) and
live execution via py-clob-client.
"""

import os
import sys
import json
import signal
import asyncio
import logging
import argparse
import uuid
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field, asdict
from typing import Optional

import aiohttp
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GAMMA_API_BASE = "https://gamma-api.polymarket.com"
CLOB_API_BASE = "https://clob.polymarket.com"
CHAIN_ID = 137
SIGNATURE_TYPE = 0  # EOA

# Strategy
ENTRY_THRESHOLD = 0.91
STOP_LOSS_THRESHOLD = 0.60
ENTRY_SIZE_USD = 500.0
ENTRY_GIVE_UP_SECS = 30    # Don't attempt entry with fewer than 30s left
MIN_BOOK_DEPTH_USD = 1000.0  # Min favored-side bid depth ($) required to enter
HOT_ZONE_START = 350       # switch to 1s polling when <= this many secs left
HOT_POLL_INTERVAL = 1      # seconds between polls in hot zone
MAIN_LOOP_INTERVAL = 60
STOP_LOSS_POLL_INTERVAL = 30
FINAL_COUNTDOWN_SECS = 30   # When secs_left <= this, poll every 5s and send Slack countdown
COUNTDOWN_POLL_INTERVAL = 5
RESOLUTION_FETCH_MAX_WAIT = 600  # Poll Gamma API for resolution outcome up to this long (sec)
RESOLUTION_FETCH_INTERVAL = 10  # Seconds between resolution API retries

# Fee model for 15m crypto markets
FEE_RATE = 0.25
FEE_EXPONENT = 2

SLUG_PATTERN = "btc-updown-15m"
TRADE_LOG_FILE = "trades.jsonl"
HEARTBEAT_INTERVAL = 60    # 1 min between Slack heartbeats
POLYMARKET_EVENT_URL = "https://polymarket.com/event"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def setup_logging(log_level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger("polytrader")
    logger.setLevel(getattr(logging, log_level.upper()))

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    fh = logging.FileHandler("polytrader.log", mode="a")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    return logger


# ---------------------------------------------------------------------------
# Slack Notifications
# ---------------------------------------------------------------------------


class SlackNotifier:
    """Fire-and-forget Slack webhook notifications."""

    def __init__(
        self, webhook_url: str, tick_webhook_url: str,
        logger: logging.Logger, dry_run: bool,
    ):
        self._url = webhook_url
        self._tick_url = tick_webhook_url
        self._logger = logger
        self._dry_run = dry_run
        self._session: Optional[aiohttp.ClientSession] = None
        self._last_heartbeat: float = 0.0
        self._last_market_slug: str = ""
        # Cumulative stats for heartbeat
        self.trades_count: int = 0
        self.total_pnl: float = 0.0
        self.wins: int = 0
        self.losses: int = 0

    @property
    def enabled(self) -> bool:
        return bool(self._url)

    @property
    def tick_enabled(self) -> bool:
        return bool(self._tick_url)

    def set_session(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    async def _post(self, payload: dict, url: Optional[str] = None) -> None:
        target = url or self._url
        if not target or self._session is None:
            return
        try:
            async with self._session.post(
                target, json=payload,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status != 200:
                    self._logger.debug(f"Slack webhook returned {resp.status}")
        except Exception as e:
            self._logger.debug(f"Slack send failed: {e}")

    async def notify_trade(self, record: "TradeRecord") -> None:
        self.trades_count += 1
        if record.pnl is not None:
            self.total_pnl += record.pnl
            if record.pnl >= 0:
                self.wins += 1
            else:
                self.losses += 1

        mode = "PAPER" if self._dry_run else "LIVE"
        icon = {
            "ENTRY": ":arrow_right:",
            "EXIT_STOP_LOSS": ":octagonal_sign:",
            "EXIT_RESOLUTION": ":checkered_flag:",
        }.get(record.action, ":memo:")

        pnl_str = f"${record.pnl:+.2f}" if record.pnl is not None else "n/a"
        poly_url = f"{POLYMARKET_EVENT_URL}/{record.market_slug}"
        text = (
            f"{icon} *{record.action}* [{mode}]\n"
            f"> Market: `{record.market_slug}` (<{poly_url}|Polymarket>)\n"
            f"> Side: *{record.side}* @ {record.price:.4f}\n"
            f"> Shares: {record.shares:.2f} | Fee: ${record.fee:.4f}\n"
            f"> PnL: *{pnl_str}* | Session total: ${self.total_pnl:+.2f} "
            f"({self.wins}W/{self.losses}L)"
        )
        if record.trade_id:
            text += f"\n> Trade ID: `{record.trade_id}`"
        if record.action == "EXIT_RESOLUTION" and record.resolution_outcome is not None:
            outcome = record.resolution_outcome
            result = "WIN" if record.resolution_won else "LOSS"
            text += f"\n> Resolution: *{outcome}* won | we had *{record.side}* → *{result}*"
        await self._post({"text": text})

    async def notify_pnl_summary(
        self, n_1h: int, pnl_1h: float, n_24h: int, pnl_24h: float,
    ) -> None:
        """Send hourly and 24h P/L summary to Slack."""
        text = (
            ":chart_with_upwards_trend: *P/L summary*\n"
            f"> Last 1h:  *{n_1h}* trades | ${pnl_1h:+.2f}\n"
            f"> Last 24h: *{n_24h}* trades | ${pnl_24h:+.2f}"
        )
        await self._post({"text": text})

    async def notify_error(self, error_msg: str) -> None:
        text = f":warning: *BOT ERROR*\n```{error_msg[:1500]}```"
        await self._post({"text": text})

    async def notify_startup(self, config: "Config") -> None:
        mode = "PAPER" if config.dry_run else "LIVE"
        text = (
            f":rocket: *Polytrader started* [{mode}]\n"
            f"> Entry threshold: {config.entry_threshold}\n"
            f"> Stop-loss: {config.stop_loss_threshold}\n"
            f"> Size: ${config.entry_size_usd}\n"
            f"> Slack: connected"
        )
        await self._post({"text": text})

    async def notify_shutdown(self, engine) -> None:
        pos_str = "none"
        if engine.has_position:
            pos = engine.position
            pos_str = f"{pos.side} x{pos.shares:.1f} @ {pos.entry_price:.4f}"
        text = (
            f":stop_sign: *Polytrader stopped*\n"
            f"> Trades: {self.trades_count} | PnL: ${self.total_pnl:+.2f} "
            f"({self.wins}W/{self.losses}L)\n"
            f"> Open position: {pos_str}"
        )
        await self._post({"text": text})

    async def maybe_heartbeat(self, market: Optional["Market"], engine) -> None:
        """Send a heartbeat if HEARTBEAT_INTERVAL has elapsed."""
        import time
        now = time.monotonic()
        if now - self._last_heartbeat < HEARTBEAT_INTERVAL:
            return
        self._last_heartbeat = now

        mode = "PAPER" if self._dry_run else "LIVE"
        market_str = "none"
        if market:
            secs = market.seconds_until_resolution
            mins, s = divmod(int(max(secs, 0)), 60)
            market_str = f"`{market.slug}` T-{mins}:{s:02d}"

        pos_str = "none"
        if engine.has_position:
            pos = engine.position
            pos_str = f"{pos.side} x{pos.shares:.1f} @ {pos.entry_price:.4f}"

        text = (
            f":chart_with_upwards_trend: *Heartbeat* [{mode}]\n"
            f"> Market: {market_str}\n"
            f"> Position: {pos_str}\n"
            f"> Session: {self.trades_count} trades | "
            f"PnL: ${self.total_pnl:+.2f} ({self.wins}W/{self.losses}L)"
        )
        await self._post({"text": text})

    async def notify_countdown(
        self, secs_left: float, market: "Market", position,
    ) -> None:
        """Send final-countdown update to main Slack (heartbeat channel). T-30s and below."""
        mode = "PAPER" if self._dry_run else "LIVE"
        secs = max(0, int(secs_left))
        text = (
            f":timer_clock: *Final countdown* [{mode}]\n"
            f"> Market: `{market.slug}`\n"
            f"> *T-{secs}s* to resolution\n"
            f"> Position: *{position.side}* x{position.shares:.1f} @ {position.entry_price:.4f} "
            f"(trade_id={position.trade_id})"
        )
        await self._post({"text": text})

    async def notify_resolution_waiting(
        self, slug: str, waited_secs: float, position_side: str,
    ) -> None:
        """Notify that we are waiting for API to return resolution outcome."""
        mode = "PAPER" if self._dry_run else "LIVE"
        poly_url = f"{POLYMARKET_EVENT_URL}/{slug}"
        text = (
            f":hourglass_flowing_sand: *Awaiting resolution* [{mode}]\n"
            f"> Market: `{slug}` (<{poly_url}|Polymarket>)\n"
            f"> Our side: *{position_side}* | Waited {waited_secs:.0f}s for API outcome"
        )
        await self._post({"text": text})

    async def notify_tick(self, ticker_line: str) -> None:
        """Send a tick update to the dedicated tick webhook channel."""
        if not self.tick_enabled:
            return
        await self._post({"text": f"`{ticker_line}`"}, url=self._tick_url)

    async def notify_market_link(
        self, market: "Market", context: str = "discovered",
    ) -> None:
        """Send a Polymarket link to the main webhook (deduplicated by slug)."""
        if market.slug == self._last_market_slug:
            return
        self._last_market_slug = market.slug
        url = f"{POLYMARKET_EVENT_URL}/{market.slug}"
        secs = market.seconds_until_resolution
        mins, s = divmod(int(max(secs, 0)), 60)
        text = (
            f":eyes: *Market {context}*\n"
            f"> `{market.slug}`\n"
            f"> Resolves in T-{mins}:{s:02d}\n"
            f"> <{url}|View on Polymarket>"
        )
        await self._post({"text": text})


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class Config:
    dry_run: bool = True
    pk: str = ""
    api_key: str = ""
    api_secret: str = ""
    api_passphrase: str = ""
    funder: str = ""
    entry_threshold: float = ENTRY_THRESHOLD
    stop_loss_threshold: float = STOP_LOSS_THRESHOLD
    entry_size_usd: float = ENTRY_SIZE_USD
    min_book_depth_usd: float = MIN_BOOK_DEPTH_USD
    log_level: str = "INFO"
    slack_webhook_url: str = ""
    slack_tick_webhook_url: str = ""

    @classmethod
    def from_env_and_args(cls) -> "Config":
        load_dotenv()

        parser = argparse.ArgumentParser(description="Polymarket BTC 15m Bot")
        parser.add_argument(
            "--dry-run", action="store_true", default=False,
            help="Paper trading mode (no real orders)",
        )
        parser.add_argument(
            "--log-level", default="INFO",
            choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        )
        parser.add_argument("--entry-threshold", type=float, default=ENTRY_THRESHOLD)
        parser.add_argument("--stop-loss", type=float, default=STOP_LOSS_THRESHOLD)
        parser.add_argument("--size", type=float, default=ENTRY_SIZE_USD)
        parser.add_argument(
            "--min-depth", type=float, default=MIN_BOOK_DEPTH_USD,
            help="Min favored-side book depth (USD) required to enter a trade",
        )
        args = parser.parse_args()

        return cls(
            dry_run=args.dry_run,
            pk=os.getenv("PK", ""),
            api_key=os.getenv("API_KEY", ""),
            api_secret=os.getenv("API_SECRET", ""),
            api_passphrase=os.getenv("API_PASSPHRASE", ""),
            funder=os.getenv("FUNDER", ""),
            entry_threshold=args.entry_threshold,
            stop_loss_threshold=args.stop_loss,
            entry_size_usd=args.size,
            min_book_depth_usd=args.min_depth,
            log_level=args.log_level,
            slack_webhook_url=os.getenv("SLACK_WEBHOOK_URL", ""),
            slack_tick_webhook_url=os.getenv("SLACK_TICK_WEBHOOK_URL", ""),
        )

    def validate(self) -> None:
        if not self.dry_run:
            missing = [
                name for name, attr in [("PK", self.pk), ("FUNDER", self.funder)]
                if not attr
            ]
            if missing:
                raise ValueError(
                    f"Live mode requires env vars: {', '.join(missing)}"
                )


# ---------------------------------------------------------------------------
# Market Discovery
# ---------------------------------------------------------------------------


@dataclass
class Market:
    market_id: str
    slug: str
    question: str
    condition_id: str
    end_date: datetime
    token_id_up: str
    token_id_down: str
    outcomes: list

    @property
    def seconds_until_resolution(self) -> float:
        return (self.end_date - datetime.now(timezone.utc)).total_seconds()



def _compute_candidate_slugs(now_ts: float, count: int = 4) -> list[str]:
    """
    Compute candidate market slugs around the current time.
    Slugs follow the pattern btc-updown-15m-{start_unix} where start_unix
    is aligned to 900-second (15-minute) intervals.
    """
    base = int(now_ts // 900) * 900
    # Check current candle and a few upcoming ones
    return [f"{SLUG_PATTERN}-{base + i * 900}" for i in range(-1, count)]


async def _fetch_market_by_slug(
    session: aiohttp.ClientSession, slug: str, logger: logging.Logger,
) -> Optional[Market]:
    url = f"{GAMMA_API_BASE}/markets"
    try:
        async with session.get(
            url, params={"slug": slug},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.debug(f"Gamma API request for {slug} failed: {e}")
        return None

    if not data:
        return None

    m = data[0]
    if m.get("closed"):
        return None

    end_str = m.get("endDate", "")
    if not end_str:
        return None
    try:
        end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
    except ValueError:
        return None

    if end_dt <= datetime.now(timezone.utc):
        return None

    raw_tokens = m.get("clobTokenIds", "[]")
    if isinstance(raw_tokens, str):
        try:
            token_ids = json.loads(raw_tokens)
        except json.JSONDecodeError:
            token_ids = []
    else:
        token_ids = raw_tokens
    if len(token_ids) < 2:
        return None
    raw_outcomes = m.get("outcomes", '["Up", "Down"]')
    if isinstance(raw_outcomes, str):
        try:
            outcomes = json.loads(raw_outcomes)
        except json.JSONDecodeError:
            outcomes = ["Up", "Down"]
    else:
        outcomes = raw_outcomes

    return Market(
        market_id=str(m.get("id", "")),
        slug=m.get("slug", slug),
        question=m.get("question", ""),
        condition_id=m.get("conditionId", ""),
        end_date=end_dt,
        token_id_up=token_ids[0],
        token_id_down=token_ids[1],
        outcomes=outcomes,
    )


@dataclass
class ResolutionInfo:
    outcome_label: str
    winning_token_id: Optional[str] = None


def _normalize_outcome_label(label: Optional[str]) -> Optional[str]:
    if not label:
        return None
    s = str(label).strip().lower()
    if s in ("up", "yes", "increase", "higher"):
        return "up"
    if s in ("down", "no", "decrease", "lower"):
        return "down"
    return s


async def _fetch_resolution_info(
    session: aiohttp.ClientSession, slug: str, logger: logging.Logger,
) -> Optional[ResolutionInfo]:
    """
    Fetch the winning outcome and winning token_id for a market by slug.
    Returns ResolutionInfo or None if unresolved/fetch failed.
    Uses Gamma API outcomePrices: resolved markets have one outcome at "1" and one at "0".
    """
    url = f"{GAMMA_API_BASE}/markets"
    try:
        async with session.get(
            url, params={"slug": slug},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.debug(f"Gamma API resolution fetch for {slug} failed: {e}")
        return None

    if not data:
        return None

    m = data[0]
    # Accept resolution prices if closed=True, OR if the endDate is in the past.
    # Polymarket can take 5-10 min to set closed=True after a market ends,
    # so waiting for that flag causes every trade to be recorded as a loss.
    is_closed = m.get("closed", False)
    end_str = m.get("endDate", "")
    market_ended = False
    if end_str:
        try:
            end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            market_ended = end_dt <= datetime.now(timezone.utc)
        except ValueError:
            pass
    if not is_closed and not market_ended:
        return None
    logger.debug(
        f"Resolution check for {slug}: closed={is_closed}, market_ended={market_ended}"
    )

    raw_prices = m.get("outcomePrices", "[]")
    raw_outcomes = m.get("outcomes", '["Up", "Down"]')
    raw_tokens = m.get("clobTokenIds", "[]")
    if isinstance(raw_prices, str):
        try:
            prices = json.loads(raw_prices)
        except json.JSONDecodeError:
            prices = []
    else:
        prices = raw_prices
    if isinstance(raw_outcomes, str):
        try:
            outcomes = json.loads(raw_outcomes)
        except json.JSONDecodeError:
            outcomes = []
    else:
        outcomes = raw_outcomes
    if isinstance(raw_tokens, str):
        try:
            token_ids = json.loads(raw_tokens)
        except json.JSONDecodeError:
            token_ids = []
    else:
        token_ids = raw_tokens

    if len(prices) < 2 or len(outcomes) < 2:
        return None

    # Resolved: one outcome is 1 (or "1"), the other 0 (or "0")
    for i, p in enumerate(prices):
        if i >= len(outcomes):
            break
        try:
            if float(p) >= 0.99:  # winning outcome
                win_token = token_ids[i] if i < len(token_ids) else None
                return ResolutionInfo(
                    outcome_label=str(outcomes[i]),
                    winning_token_id=str(win_token) if win_token is not None else None,
                )
        except (TypeError, ValueError):
            if str(p).strip() == "1":
                win_token = token_ids[i] if i < len(token_ids) else None
                return ResolutionInfo(
                    outcome_label=str(outcomes[i]),
                    winning_token_id=str(win_token) if win_token is not None else None,
                )
    return None


async def _fetch_resolution_with_retry(
    session: aiohttp.ClientSession,
    slug: str,
    logger: logging.Logger,
    shutdown_event: asyncio.Event,
    notifier: Optional["SlackNotifier"] = None,
    position_side: Optional[str] = None,
    max_wait_secs: float = RESOLUTION_FETCH_MAX_WAIT,
    interval_secs: float = RESOLUTION_FETCH_INTERVAL,
) -> Optional[ResolutionInfo]:
    """
    Poll Gamma API for resolution outcome/token until we get it or max_wait_secs.
    Polymarket can delay setting closed=true and outcomePrices; do not assume loss
    if the first fetch fails.
    """
    elapsed = 0.0
    last_notified_bucket = -1
    while elapsed < max_wait_secs and not shutdown_event.is_set():
        info = await _fetch_resolution_info(session, slug, logger)
        if info is not None:
            logger.info(
                f"Resolution outcome from API: {info.outcome_label} "
                f"(token={info.winning_token_id}) after {elapsed:.0f}s wait"
            )
            return info
        logger.info(
            f"Resolution not yet on API (waited {elapsed:.0f}s) — retrying in {interval_secs}s"
        )
        if notifier and notifier.enabled and position_side is not None:
            # Notify every 30s bucket to avoid noisy spam.
            bucket = int(elapsed // 30)
            if bucket > last_notified_bucket:
                last_notified_bucket = bucket
                await notifier.notify_resolution_waiting(slug, elapsed, position_side)
        await _interruptible_sleep(interval_secs, shutdown_event)
        elapsed += interval_secs
    logger.warning(
        f"Resolution outcome still not available after {elapsed:.0f}s — will assume loss"
    )
    return None


async def discover_market(
    session: aiohttp.ClientSession, logger: logging.Logger,
) -> Optional[Market]:
    """
    Discover the current BTC 15m market by computing candidate slugs
    from the current timestamp and querying the Gamma API for each.
    Returns the market resolving soonest that is still open.
    """
    now_ts = datetime.now(timezone.utc).timestamp()
    slugs = _compute_candidate_slugs(now_ts)
    logger.debug(f"Trying slugs: {slugs}")

    # Fetch all candidates in parallel
    tasks = [_fetch_market_by_slug(session, s, logger) for s in slugs]
    results = await asyncio.gather(*tasks)

    candidates = [m for m in results if m is not None]

    if not candidates:
        logger.warning("No active BTC 15m markets found")
        return None

    # Pick the one resolving soonest (the current candle)
    candidates.sort(key=lambda c: c.end_date)
    chosen = candidates[0]
    logger.info(
        f"Market: {chosen.slug} | resolves {chosen.end_date.isoformat()} | "
        f"{chosen.seconds_until_resolution:.0f}s left"
    )
    return chosen


# ---------------------------------------------------------------------------
# Order Book Analysis
# ---------------------------------------------------------------------------


@dataclass
class OrderBookSignal:
    favored_side: str
    favored_token_id: str
    best_bid: float
    opposing_best_bid: float
    book_depth_favored: float
    book_depth_opposing: float
    is_entry_worthy: bool
    up_best_bid: float
    down_best_bid: float


async def _fetch_book_http(
    session: aiohttp.ClientSession, token_id: str,
) -> dict:
    url = f"{CLOB_API_BASE}/book"
    params = {"token_id": token_id}
    try:
        async with session.get(
            url, params=params, timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                return {"bids": [], "asks": []}
            return await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return {"bids": [], "asks": []}


def _extract_best_bid(book: dict) -> Optional[float]:
    bids = book.get("bids", [])
    if not bids:
        return None
    return max(float(b.get("price", 0)) for b in bids)


def _sum_bid_depth(book: dict) -> float:
    total = 0.0
    for b in book.get("bids", []):
        total += float(b.get("price", 0)) * float(b.get("size", 0))
    return total


async def analyze_order_book(
    market: Market,
    session: aiohttp.ClientSession,
    clob_client,
    logger: logging.Logger,
    dry_run: bool,
    entry_threshold: float,
) -> Optional[OrderBookSignal]:
    try:
        if dry_run or clob_client is None:
            up_book, down_book = await asyncio.gather(
                _fetch_book_http(session, market.token_id_up),
                _fetch_book_http(session, market.token_id_down),
            )
        else:
            loop = asyncio.get_event_loop()
            up_book = await loop.run_in_executor(
                None, clob_client.get_order_book, market.token_id_up,
            )
            down_book = await loop.run_in_executor(
                None, clob_client.get_order_book, market.token_id_down,
            )
    except Exception as e:
        logger.error(f"Failed to fetch order book: {e}")
        return None

    up_best = _extract_best_bid(up_book) or 0.0
    down_best = _extract_best_bid(down_book) or 0.0

    if up_best == 0.0 and down_best == 0.0:
        logger.warning("Both order books empty")
        return None

    if up_best >= down_best:
        favored, fav_tid, best, opp = "Up", market.token_id_up, up_best, down_best
        depth_fav = _sum_bid_depth(up_book)
        depth_opp = _sum_bid_depth(down_book)
    else:
        favored, fav_tid, best, opp = "Down", market.token_id_down, down_best, up_best
        depth_fav = _sum_bid_depth(down_book)
        depth_opp = _sum_bid_depth(up_book)

    sig = OrderBookSignal(
        favored_side=favored,
        favored_token_id=fav_tid,
        best_bid=best,
        opposing_best_bid=opp,
        book_depth_favored=depth_fav,
        book_depth_opposing=depth_opp,
        is_entry_worthy=(best >= entry_threshold),
        up_best_bid=up_best,
        down_best_bid=down_best,
    )
    logger.info(
        f"Signal: {favored} @ {best:.4f} | opp @ {opp:.4f} | "
        f"depth=${depth_fav:.2f} | entry_worthy={sig.is_entry_worthy}"
    )
    return sig


# ---------------------------------------------------------------------------
# Fee & Share Math
# ---------------------------------------------------------------------------


def compute_fee(shares: float, price: float) -> float:
    p = min(max(price, 0.001), 0.999)
    return shares * FEE_RATE * ((p * (1.0 - p)) ** FEE_EXPONENT)


def compute_shares(usd_amount: float, price: float) -> float:
    p = min(max(price, 0.001), 0.999)
    fee_per_share = FEE_RATE * ((p * (1.0 - p)) ** FEE_EXPONENT)
    return usd_amount / (price + fee_per_share)


# ---------------------------------------------------------------------------
# Position & Trade Logging
# ---------------------------------------------------------------------------


def _new_trade_id() -> str:
    """Unique ID for a single round-trip (entry + its exit). Stored in Position and all TradeRecords for that trade."""
    return uuid.uuid4().hex[:12]


@dataclass
class Position:
    trade_id: str
    market_slug: str
    side: str
    token_id: str
    entry_price: float
    shares: float
    entry_time: datetime
    entry_fee: float
    entry_seconds_remaining: Optional[float] = None
    market_id: Optional[str] = None
    condition_id: Optional[str] = None

    @property
    def cost_basis(self) -> float:
        return self.entry_price * self.shares + self.entry_fee


@dataclass
class TradeRecord:
    timestamp: str
    market_slug: str
    side: str
    action: str
    price: float
    shares: float
    fee: float
    pnl: Optional[float]
    dry_run: bool
    # Trade tracking: same id from entry through exit (stop-loss or resolution)
    trade_id: Optional[str] = None
    # Resolution tracking (EXIT_RESOLUTION only): which outcome won, did we win
    resolution_outcome: Optional[str] = None  # "Up" or "Down"
    resolution_won: Optional[bool] = None
    # ML analytics fields
    seconds_remaining: Optional[float] = None
    up_best_bid: Optional[float] = None
    down_best_bid: Optional[float] = None
    book_depth_favored: Optional[float] = None
    book_depth_opposing: Optional[float] = None
    opposing_best_bid: Optional[float] = None
    market_id: Optional[str] = None
    condition_id: Optional[str] = None
    # Exit-only: reference back to entry
    entry_price: Optional[float] = None
    entry_seconds_remaining: Optional[float] = None


def _compute_pnl_for_window(
    since_utc: datetime,
) -> tuple[int, float]:
    """
    Read trades.jsonl and return (exit_trade_count, total_pnl) for trades
    with timestamp >= since_utc. Only EXIT_STOP_LOSS and EXIT_RESOLUTION count.
    """
    count = 0
    total_pnl = 0.0
    if not os.path.isfile(TRADE_LOG_FILE):
        return 0, 0.0
    try:
        with open(TRADE_LOG_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                action = row.get("action", "")
                if action not in ("EXIT_STOP_LOSS", "EXIT_RESOLUTION"):
                    continue
                ts_str = row.get("timestamp")
                if not ts_str:
                    continue
                try:
                    ts = datetime.fromisoformat(
                        ts_str.replace("Z", "+00:00")
                    )
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                except (ValueError, TypeError):
                    continue
                if ts < since_utc:
                    continue
                pnl = row.get("pnl")
                if pnl is not None:
                    count += 1
                    total_pnl += float(pnl)
    except OSError:
        pass
    return count, total_pnl


def log_trade(
    record: TradeRecord, logger: logging.Logger,
    notifier: Optional[SlackNotifier] = None,
) -> None:
    with open(TRADE_LOG_FILE, "a") as f:
        f.write(json.dumps(asdict(record)) + "\n")
    tid = f" | trade_id={record.trade_id}" if record.trade_id else ""
    res = ""
    if record.action == "EXIT_RESOLUTION" and record.resolution_outcome is not None:
        res = f" | resolution={record.resolution_outcome} won={record.resolution_won}"
    logger.info(
        f"TRADE | {record.action} | {record.side} @ {record.price:.4f} | "
        f"shares={record.shares:.2f} | fee={record.fee:.4f} | pnl={record.pnl}{tid}{res}"
    )
    # P/L updates go to Slack via notify_trade (not duplicated to terminal)
    if notifier:
        asyncio.ensure_future(notifier.notify_trade(record))


def _compute_pnl_summaries() -> tuple[int, float, int, float]:
    """Return (n_1h, pnl_1h, n_24h, pnl_24h) for exit trades in last 1h and 24h."""
    now = datetime.now(timezone.utc)
    one_hour_ago = now - timedelta(hours=1)
    one_day_ago = now - timedelta(hours=24)
    n1, pnl1 = _compute_pnl_for_window(one_hour_ago)
    n24, pnl24 = _compute_pnl_for_window(one_day_ago)
    return n1, pnl1, n24, pnl24


async def _pnl_summary_loop(
    logger: logging.Logger,
    shutdown_event: asyncio.Event,
    notifier: Optional["SlackNotifier"] = None,
    interval_seconds: float = 3600.0,
) -> None:
    """Every interval_seconds, send 1h and 24h P/L summaries to Slack (and log at DEBUG)."""
    while not shutdown_event.is_set():
        try:
            await asyncio.wait_for(
                shutdown_event.wait(), timeout=interval_seconds
            )
        except asyncio.TimeoutError:
            pass
        if shutdown_event.is_set():
            break
        n1, pnl1, n24, pnl24 = _compute_pnl_summaries()
        logger.debug(
            f"P/L summary [1h]: {n1} trades | ${pnl1:+.2f} | [24h]: {n24} trades | ${pnl24:+.2f}"
        )
        if notifier and notifier.enabled:
            await notifier.notify_pnl_summary(n1, pnl1, n24, pnl24)


# ---------------------------------------------------------------------------
# Paper Trade Engine
# ---------------------------------------------------------------------------


class PaperTradeEngine:
    def __init__(self, logger: logging.Logger, notifier: Optional[SlackNotifier] = None):
        self.logger = logger
        self.notifier = notifier
        self.position: Optional[Position] = None

    async def enter(
        self, market: Market, signal: OrderBookSignal, config: Config,
    ) -> bool:
        price = signal.best_bid
        shares = compute_shares(config.entry_size_usd, price)
        fee = compute_fee(shares, price)

        trade_id = _new_trade_id()
        secs_remaining = market.seconds_until_resolution
        self.position = Position(
            trade_id=trade_id,
            market_slug=market.slug,
            side=signal.favored_side,
            token_id=signal.favored_token_id,
            entry_price=price,
            shares=shares,
            entry_time=datetime.now(timezone.utc),
            entry_fee=fee,
            entry_seconds_remaining=secs_remaining,
            market_id=market.market_id,
            condition_id=market.condition_id,
        )

        log_trade(TradeRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            market_slug=market.slug,
            side=signal.favored_side,
            action="ENTRY",
            price=price,
            shares=shares,
            fee=fee,
            pnl=None,
            dry_run=True,
            trade_id=trade_id,
            seconds_remaining=secs_remaining,
            up_best_bid=signal.up_best_bid,
            down_best_bid=signal.down_best_bid,
            book_depth_favored=signal.book_depth_favored,
            book_depth_opposing=signal.book_depth_opposing,
            opposing_best_bid=signal.opposing_best_bid,
            market_id=market.market_id,
            condition_id=market.condition_id,
        ), self.logger, self.notifier)
        return True

    async def exit_stop_loss(
        self, current_price: float, market: Optional["Market"] = None,
    ) -> None:
        if self.position is None:
            return
        pos = self.position
        exit_fee = compute_fee(pos.shares, current_price)
        revenue = pos.shares * current_price - exit_fee
        pnl = revenue - pos.cost_basis

        log_trade(TradeRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            market_slug=pos.market_slug,
            side=pos.side,
            action="EXIT_STOP_LOSS",
            price=current_price,
            shares=pos.shares,
            fee=exit_fee,
            pnl=pnl,
            dry_run=True,
            trade_id=pos.trade_id,
            seconds_remaining=market.seconds_until_resolution if market else None,
            market_id=pos.market_id,
            condition_id=pos.condition_id,
            entry_price=pos.entry_price,
            entry_seconds_remaining=pos.entry_seconds_remaining,
        ), self.logger, self.notifier)
        self.position = None

    async def exit_resolution(
        self, won: bool, resolution_outcome: Optional[str] = None,
    ) -> None:
        if self.position is None:
            return
        pos = self.position
        payout = pos.shares * (1.0 if won else 0.0)
        pnl = payout - pos.cost_basis

        log_trade(TradeRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            market_slug=pos.market_slug,
            side=pos.side,
            action="EXIT_RESOLUTION",
            price=1.0 if won else 0.0,
            shares=pos.shares,
            fee=0.0,
            pnl=pnl,
            dry_run=True,
            trade_id=pos.trade_id,
            resolution_outcome=resolution_outcome,
            resolution_won=won,
            seconds_remaining=0.0,
            market_id=pos.market_id,
            condition_id=pos.condition_id,
            entry_price=pos.entry_price,
            entry_seconds_remaining=pos.entry_seconds_remaining,
        ), self.logger, self.notifier)
        self.position = None

    @property
    def has_position(self) -> bool:
        return self.position is not None


# ---------------------------------------------------------------------------
# Live Trade Engine
# ---------------------------------------------------------------------------


class LiveTradeEngine:
    def __init__(self, config: Config, logger: logging.Logger, notifier: Optional[SlackNotifier] = None):
        self.logger = logger
        self.config = config
        self.notifier = notifier
        self.position: Optional[Position] = None
        self._client = None

    async def _get_client(self):
        if self._client is not None:
            return self._client
        loop = asyncio.get_event_loop()
        client = await loop.run_in_executor(None, self._init_client)
        self._client = client
        return client

    def _init_client(self):
        from py_clob_client.client import ClobClient

        client = ClobClient(
            CLOB_API_BASE,
            key=self.config.pk,
            chain_id=CHAIN_ID,
            signature_type=SIGNATURE_TYPE,
            funder=self.config.funder,
        )

        if (
            self.config.api_key
            and self.config.api_secret
            and self.config.api_passphrase
        ):
            from py_clob_client.clob_types import ApiCreds

            client.set_api_creds(ApiCreds(
                api_key=self.config.api_key,
                api_secret=self.config.api_secret,
                api_passphrase=self.config.api_passphrase,
            ))
        else:
            creds = client.create_or_derive_api_creds()
            client.set_api_creds(creds)

        self.logger.info("ClobClient initialized successfully")
        return client

    async def enter(
        self, market: Market, signal: OrderBookSignal, config: Config,
    ) -> bool:
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY

        client = await self._get_client()
        price = signal.best_bid
        shares = compute_shares(config.entry_size_usd, price)
        fee = compute_fee(shares, price)
        rounded_shares = round(shares, 2)

        loop = asyncio.get_event_loop()
        try:
            order_args = OrderArgs(
                token_id=signal.favored_token_id,
                price=price,
                size=rounded_shares,
                side=BUY,
            )
            signed = await loop.run_in_executor(
                None, client.create_order, order_args,
            )
            resp = await loop.run_in_executor(
                None, lambda: client.post_order(signed, OrderType.GTC),
            )
            self.logger.info(f"Order posted: {resp}")
        except Exception as e:
            self.logger.error(f"Failed to place entry order: {e}")
            return False

        trade_id = _new_trade_id()
        secs_remaining = market.seconds_until_resolution
        self.position = Position(
            trade_id=trade_id,
            market_slug=market.slug,
            side=signal.favored_side,
            token_id=signal.favored_token_id,
            entry_price=price,
            shares=rounded_shares,
            entry_time=datetime.now(timezone.utc),
            entry_fee=fee,
            entry_seconds_remaining=secs_remaining,
            market_id=market.market_id,
            condition_id=market.condition_id,
        )

        log_trade(TradeRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            market_slug=market.slug,
            side=signal.favored_side,
            action="ENTRY",
            price=price,
            shares=rounded_shares,
            fee=fee,
            pnl=None,
            dry_run=False,
            trade_id=trade_id,
            seconds_remaining=secs_remaining,
            up_best_bid=signal.up_best_bid,
            down_best_bid=signal.down_best_bid,
            book_depth_favored=signal.book_depth_favored,
            book_depth_opposing=signal.book_depth_opposing,
            opposing_best_bid=signal.opposing_best_bid,
            market_id=market.market_id,
            condition_id=market.condition_id,
        ), self.logger, self.notifier)
        return True

    async def exit_stop_loss(
        self, current_price: float, market: Optional["Market"] = None,
    ) -> None:
        if self.position is None:
            return
        from py_clob_client.clob_types import MarketOrderArgs, OrderType
        from py_clob_client.order_builder.constants import SELL

        pos = self.position
        client = await self._get_client()
        loop = asyncio.get_event_loop()

        try:
            market_args = MarketOrderArgs(
                token_id=pos.token_id,
                amount=round(pos.shares * current_price, 2),
                side=SELL,
            )
            signed = await loop.run_in_executor(
                None, client.create_market_order, market_args,
            )
            resp = await loop.run_in_executor(
                None, lambda: client.post_order(signed, OrderType.FAK),
            )
            self.logger.info(f"Stop-loss FAK response: {resp}")
        except Exception as e:
            self.logger.error(f"Stop-loss order failed: {e}")
            return

        exit_fee = compute_fee(pos.shares, current_price)
        revenue = pos.shares * current_price - exit_fee
        pnl = revenue - pos.cost_basis

        log_trade(TradeRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            market_slug=pos.market_slug,
            side=pos.side,
            action="EXIT_STOP_LOSS",
            price=current_price,
            shares=pos.shares,
            fee=exit_fee,
            pnl=pnl,
            dry_run=False,
            trade_id=pos.trade_id,
            seconds_remaining=market.seconds_until_resolution if market else None,
            market_id=pos.market_id,
            condition_id=pos.condition_id,
            entry_price=pos.entry_price,
            entry_seconds_remaining=pos.entry_seconds_remaining,
        ), self.logger, self.notifier)
        self.position = None

    async def exit_resolution(
        self, won: bool, resolution_outcome: Optional[str] = None,
    ) -> None:
        if self.position is None:
            return
        pos = self.position
        payout = pos.shares * (1.0 if won else 0.0)
        pnl = payout - pos.cost_basis

        log_trade(TradeRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            market_slug=pos.market_slug,
            side=pos.side,
            action="EXIT_RESOLUTION",
            price=1.0 if won else 0.0,
            shares=pos.shares,
            fee=0.0,
            pnl=pnl,
            dry_run=False,
            trade_id=pos.trade_id,
            resolution_outcome=resolution_outcome,
            resolution_won=won,
            seconds_remaining=0.0,
            market_id=pos.market_id,
            condition_id=pos.condition_id,
            entry_price=pos.entry_price,
            entry_seconds_remaining=pos.entry_seconds_remaining,
        ), self.logger, self.notifier)
        self.position = None

    @property
    def has_position(self) -> bool:
        return self.position is not None


# ---------------------------------------------------------------------------
# Stop-Loss Monitor
# ---------------------------------------------------------------------------


async def _interruptible_sleep(seconds: float, event: asyncio.Event) -> None:
    try:
        await asyncio.wait_for(event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


def _log_loss_postmortem(
    logger: logging.Logger,
    position: "Position",
    price_snapshots: list,
    notifier: Optional["SlackNotifier"] = None,
) -> None:
    """Log a price-movement post-mortem when a trade resolves as a loss."""
    entry = position.entry_price
    if not price_snapshots:
        logger.warning(
            f"LOSS POST-MORTEM | {position.side} entry={entry:.4f} | no price history recorded"
        )
        return

    # Summarise the price journey
    first_secs, first_bid, first_depth = price_snapshots[0]
    last_secs, last_bid, last_depth = price_snapshots[-1]
    peak_bid = max(s[1] for s in price_snapshots)
    trough_bid = min(s[1] for s in price_snapshots)
    peak_depth = max(s[2] for s in price_snapshots)
    trough_depth = min(s[2] for s in price_snapshots)

    lines = [
        f"LOSS POST-MORTEM | Side={position.side} | entry={entry:.4f}",
        f"  Price journey: {first_bid:.4f} @ T-{first_secs:.0f}s  →  {last_bid:.4f} @ T-{last_secs:.0f}s",
        f"  Peak={peak_bid:.4f}  Trough={trough_bid:.4f}  Drift={last_bid - first_bid:+.4f}",
        f"  Depth: peak=${peak_depth:.0f}  trough=${trough_depth:.0f}  final=${last_depth:.0f}",
        f"  Snapshots ({len(price_snapshots)}):",
    ]
    for secs, bid, depth in price_snapshots:
        delta = bid - entry
        lines.append(f"    T-{secs:5.0f}s  bid={bid:.4f} ({delta:+.4f} vs entry)  depth=${depth:.0f}")
    msg = "\n".join(lines)
    logger.warning(msg)
    if notifier and notifier.enabled:
        slack_body = (
            ":microscope: *LOSS POST-MORTEM*\n"
            f"> Side: *{position.side}* | Entry: {entry:.4f}\n"
            f"> Price drift: {first_bid:.4f} → {last_bid:.4f} "
            f"({last_bid - first_bid:+.4f} net)\n"
            f"> Peak: {peak_bid:.4f} | Trough: {trough_bid:.4f}\n"
            f"> Depth: peak=${peak_depth:.0f} | trough=${trough_depth:.0f} | "
            f"final=${last_depth:.0f}\n"
            f"> Snapshots: {len(price_snapshots)} polls over the holding period"
        )
        import asyncio as _asyncio
        try:
            loop = _asyncio.get_event_loop()
            if loop.is_running():
                _asyncio.ensure_future(notifier._post({"text": slack_body}))
        except Exception:
            pass


async def run_stop_loss_monitor(
    engine,
    market: Market,
    session: aiohttp.ClientSession,
    clob_client,
    config: Config,
    logger: logging.Logger,
    shutdown_event: asyncio.Event,
    notifier: Optional["SlackNotifier"] = None,
) -> str:
    logger.info("Stop-loss monitor started")

    # Collect (secs_left, bid, depth) snapshots throughout the hold for post-mortem
    price_snapshots: list[tuple[float, float, float]] = []

    while engine.has_position and not shutdown_event.is_set():
        secs_left = market.seconds_until_resolution

        # Final 30s: poll every 5s and send countdown to main Slack
        poll_interval = (
            COUNTDOWN_POLL_INTERVAL
            if secs_left <= FINAL_COUNTDOWN_SECS and secs_left > 0
            else STOP_LOSS_POLL_INTERVAL
        )
        if secs_left <= FINAL_COUNTDOWN_SECS and secs_left > 0:
            if notifier and notifier.enabled:
                await notifier.notify_countdown(secs_left, market, engine.position)

        if secs_left <= 0:
            logger.info("Market resolved while in position — fetching outcome from API (may take several min)")
            if notifier and notifier.enabled:
                await notifier.notify_resolution_waiting(
                    market.slug, 0.0, engine.position.side,
                )
            resolution = await _fetch_resolution_with_retry(
                session, market.slug, logger, shutdown_event,
                notifier=notifier,
                position_side=engine.position.side,
                max_wait_secs=RESOLUTION_FETCH_MAX_WAIT,
                interval_secs=RESOLUTION_FETCH_INTERVAL,
            )
            winning_outcome = resolution.outcome_label if resolution else None
            winning_token_id = resolution.winning_token_id if resolution else None
            if winning_token_id is not None:
                won = str(engine.position.token_id) == str(winning_token_id)
            elif winning_outcome is not None:
                won = (
                    _normalize_outcome_label(engine.position.side)
                    == _normalize_outcome_label(winning_outcome)
                )
            else:
                won = False
            if resolution is None:
                logger.warning(
                    "Could not fetch resolution outcome from API after retries; accounting as loss"
                )
            else:
                logger.info(
                    f"Resolution: {winning_outcome} won (token={winning_token_id}) | "
                    f"we had {engine.position.side} token={engine.position.token_id} -> "
                    f"{'WIN' if won else 'LOSS'}"
                )
            if not won:
                _log_loss_postmortem(logger, engine.position, price_snapshots, notifier)
            await engine.exit_resolution(won, resolution_outcome=winning_outcome)
            return "resolution"

        try:
            if config.dry_run:
                book = await _fetch_book_http(session, engine.position.token_id)
            else:
                loop = asyncio.get_event_loop()
                book = await loop.run_in_executor(
                    None, clob_client.get_order_book, engine.position.token_id,
                )
            current_bid = _extract_best_bid(book)
            current_depth = _sum_bid_depth(book)
        except Exception as e:
            logger.warning(f"Stop-loss price fetch failed: {e}")
            await _interruptible_sleep(poll_interval, shutdown_event)
            continue

        if current_bid is None:
            logger.warning("Order book empty during stop-loss check")
            await _interruptible_sleep(poll_interval, shutdown_event)
            continue

        # Record snapshot for post-mortem
        price_snapshots.append((secs_left, current_bid, current_depth))

        logger.debug(
            f"Stop-loss check: bid={current_bid:.4f} depth=${current_depth:.0f} | "
            f"threshold={config.stop_loss_threshold}"
        )

        if current_bid < config.stop_loss_threshold:
            logger.warning(
                f"STOP-LOSS TRIGGERED: {current_bid:.4f} < "
                f"{config.stop_loss_threshold}"
            )
            _log_loss_postmortem(logger, engine.position, price_snapshots, notifier)
            await engine.exit_stop_loss(current_bid, market)
            return "stop_loss"

        await _interruptible_sleep(poll_interval, shutdown_event)

    if shutdown_event.is_set():
        return "shutdown"
    return "no_position"


# ---------------------------------------------------------------------------
# Main Loop
# ---------------------------------------------------------------------------



async def _fetch_prices(
    market: Market,
    session: aiohttp.ClientSession,
    clob_client,
    dry_run: bool,
) -> tuple[float, float, float, float]:
    """Return (up_bid, down_bid, up_depth, down_depth)."""
    if dry_run or clob_client is None:
        up_book, down_book = await asyncio.gather(
            _fetch_book_http(session, market.token_id_up),
            _fetch_book_http(session, market.token_id_down),
        )
    else:
        loop = asyncio.get_event_loop()
        up_book = await loop.run_in_executor(
            None, clob_client.get_order_book, market.token_id_up,
        )
        down_book = await loop.run_in_executor(
            None, clob_client.get_order_book, market.token_id_down,
        )
    up_bid = _extract_best_bid(up_book) or 0.0
    down_bid = _extract_best_bid(down_book) or 0.0
    return up_bid, down_bid, _sum_bid_depth(up_book), _sum_bid_depth(down_book)


def _format_ticker(
    up_bid: float, down_bid: float,
    up_depth: float, down_depth: float,
    secs: float, config: Config,
    engine=None,
) -> str:
    """Build a single-line ticker string with threshold debug info."""
    mins, s = divmod(int(max(secs, 0)), 60)

    # Determine favored side
    if up_bid >= down_bid:
        fav, fav_bid, opp, opp_bid = "Up", up_bid, "Down", down_bid
    else:
        fav, fav_bid, opp, opp_bid = "Down", down_bid, "Up", up_bid

    # Threshold comparison
    delta = fav_bid - config.entry_threshold
    if fav_bid >= config.entry_threshold:
        thresh_str = f"ABOVE thresh by +{delta:.4f}"
    else:
        thresh_str = f"below thresh by {delta:.4f}"

    # Entry status
    if secs > ENTRY_GIVE_UP_SECS:
        window_str = "ENTRY ACTIVE"
    else:
        window_str = f"entry closed ({secs:.0f}s left)"

    line = (
        f"TICK | Up={up_bid:.4f} (${up_depth:.0f}) | "
        f"Down={down_bid:.4f} (${down_depth:.0f}) | "
        f"FAV={fav} @ {fav_bid:.4f} vs {config.entry_threshold} ({thresh_str}) | "
        f"T-{mins}:{s:02d} | {window_str}"
    )

    # Position info
    if engine and engine.has_position:
        pos = engine.position
        cur_price = up_bid if pos.side == "Up" else down_bid
        unrealized = pos.shares * cur_price - pos.cost_basis
        sl_delta = cur_price - config.stop_loss_threshold
        line += (
            f" | POS: {pos.side} x{pos.shares:.1f} @ {pos.entry_price:.4f} "
            f"now={cur_price:.4f} unrealPnL=${unrealized:+.2f} "
            f"SL={config.stop_loss_threshold} (margin={sl_delta:+.4f})"
        )

    return line


async def _run_one_cycle(
    engine,
    session: aiohttp.ClientSession,
    clob_client,
    config: Config,
    logger: logging.Logger,
    shutdown_event: asyncio.Event,
    notifier: Optional[SlackNotifier] = None,
) -> None:
    market = await discover_market(session, logger)
    if market is None:
        logger.info(f"No market found -- sleeping {MAIN_LOOP_INTERVAL}s")
        await _interruptible_sleep(MAIN_LOOP_INTERVAL, shutdown_event)
        return

    # Notify Slack on new market discovery (deduplicated by slug)
    if notifier:
        await notifier.notify_market_link(market)

    secs = market.seconds_until_resolution

    # Already holding a position for this market -> monitor stop-loss
    if engine.has_position and engine.position.market_slug == market.slug:
        logger.info("Existing position -- entering stop-loss monitor")
        result = await run_stop_loss_monitor(
            engine, market, session, clob_client, config, logger, shutdown_event,
            notifier=notifier,
        )
        logger.info(f"Stop-loss monitor exited: {result}")
        return

    # Holding position for a previous (resolved) market -> finalize with actual outcome
    if engine.has_position and engine.position.market_slug != market.slug:
        prev_slug = engine.position.market_slug
        logger.info(
            f"Previous market {prev_slug} resolved. New market: {market.slug}"
        )
        if notifier and notifier.enabled:
            await notifier.notify_resolution_waiting(
                prev_slug, 0.0, engine.position.side,
            )
        resolution = await _fetch_resolution_with_retry(
            session, prev_slug, logger, shutdown_event,
            notifier=notifier,
            position_side=engine.position.side,
            max_wait_secs=RESOLUTION_FETCH_MAX_WAIT,
            interval_secs=RESOLUTION_FETCH_INTERVAL,
        )
        winning_outcome = resolution.outcome_label if resolution else None
        winning_token_id = resolution.winning_token_id if resolution else None
        if winning_token_id is not None:
            won = str(engine.position.token_id) == str(winning_token_id)
        elif winning_outcome is not None:
            won = (
                _normalize_outcome_label(engine.position.side)
                == _normalize_outcome_label(winning_outcome)
            )
        else:
            won = False
        if resolution is None:
            logger.warning(
                "Could not fetch resolution outcome for previous market after retries; "
                "accounting as loss"
            )
        else:
            logger.info(
                f"Resolution for {prev_slug}: {winning_outcome} won "
                f"(token={winning_token_id}) | we had {engine.position.side} "
                f"token={engine.position.token_id} -> {'WIN' if won else 'LOSS'}"
            )
        await engine.exit_resolution(won, resolution_outcome=winning_outcome)

    # --- Far from hot zone: log price, check entry, sleep 60s ---
    if secs > HOT_ZONE_START:
        try:
            up_bid, down_bid, up_depth, down_depth = await _fetch_prices(
                market, session, clob_client, config.dry_run,
            )
            tick_line = _format_ticker(
                up_bid, down_bid, up_depth, down_depth, secs, config, engine,
            )
            logger.info(tick_line)

            # Entry check in far zone
            if not engine.has_position:
                if up_bid >= down_bid:
                    fav_side, fav_tid, fav_bid = "Up", market.token_id_up, up_bid
                    opp_bid, depth_fav, depth_opp = down_bid, up_depth, down_depth
                else:
                    fav_side, fav_tid, fav_bid = "Down", market.token_id_down, down_bid
                    opp_bid, depth_fav, depth_opp = up_bid, down_depth, up_depth

                if fav_bid >= config.entry_threshold:
                    if depth_fav < config.min_book_depth_usd:
                        logger.info(
                            f"DEPTH FILTER (far zone): {fav_side} @ {fav_bid:.4f} threshold met "
                            f"but depth=${depth_fav:.0f} < min ${config.min_book_depth_usd:.0f} "
                            f"| opp depth=${depth_opp:.0f} — skipping entry"
                        )
                    else:
                        logger.info(
                            f"*** ENTRY SIGNAL (far zone) *** {fav_side} @ {fav_bid:.4f} >= "
                            f"{config.entry_threshold} | depth=${depth_fav:.0f} (min=${config.min_book_depth_usd:.0f})"
                        )
                        sig = OrderBookSignal(
                            favored_side=fav_side,
                            favored_token_id=fav_tid,
                            best_bid=fav_bid,
                            opposing_best_bid=opp_bid,
                            book_depth_favored=depth_fav,
                            book_depth_opposing=depth_opp,
                            is_entry_worthy=True,
                            up_best_bid=up_bid,
                            down_best_bid=down_bid,
                        )
                        success = await engine.enter(market, sig, config)
                        if success:
                            logger.info("Entry successful (far zone) -- switching to stop-loss monitor")
                            if notifier:
                                await notifier.notify_market_link(market, context="entered position")
                            result = await run_stop_loss_monitor(
                                engine, market, session, clob_client, config,
                                logger, shutdown_event,
                                notifier=notifier,
                            )
                            logger.info(f"Stop-loss monitor exited: {result}")
                            return
                        else:
                            logger.error("Entry failed (far zone) -- will retry next cycle")

        except Exception as e:
            logger.debug(f"Price fetch failed: {e}")
        sleep_time = min(secs - HOT_ZONE_START + 5, MAIN_LOOP_INTERVAL)
        logger.info(f"Sleeping {sleep_time:.0f}s until hot zone")
        await _interruptible_sleep(sleep_time, shutdown_event)
        return

    # --- Hot zone (<=350s): poll every second ---
    logger.info(
        f"=== ENTERING HOT ZONE === {secs:.0f}s left | "
        f"entry active (give-up at {ENTRY_GIVE_UP_SECS}s) | "
        f"threshold={config.entry_threshold} | "
        f"stop_loss={config.stop_loss_threshold}"
    )

    while not shutdown_event.is_set():
        secs = market.seconds_until_resolution

        # Market resolved or past entry window with no position
        if secs <= 0:
            logger.info("Market resolved -- moving to next candle")
            return

        # Too close to resolution to enter -> give up
        if secs < ENTRY_GIVE_UP_SECS and not engine.has_position:
            logger.info(
                f"Too close to resolution ({secs:.0f}s left). "
                f"No entry taken. Waiting for next market."
            )
            await _interruptible_sleep(MAIN_LOOP_INTERVAL, shutdown_event)
            return

        # Fetch prices
        try:
            up_bid, down_bid, up_depth, down_depth = await _fetch_prices(
                market, session, clob_client, config.dry_run,
            )
        except Exception as e:
            logger.warning(f"Hot zone price fetch failed: {e}")
            await _interruptible_sleep(HOT_POLL_INTERVAL, shutdown_event)
            continue

        # Log ticker every tick
        tick_line = _format_ticker(
            up_bid, down_bid, up_depth, down_depth, secs, config, engine,
        )
        logger.info(tick_line)

        # --- Entry logic (threshold met, no existing position) ---
        if not engine.has_position:
            # Determine favored side
            if up_bid >= down_bid:
                fav_side, fav_tid, fav_bid = "Up", market.token_id_up, up_bid
                opp_bid = down_bid
            else:
                fav_side, fav_tid, fav_bid = "Down", market.token_id_down, down_bid
                opp_bid = up_bid

            if fav_bid >= config.entry_threshold:
                depth_fav = up_depth if fav_side == "Up" else down_depth
                depth_opp = down_depth if fav_side == "Up" else up_depth
                if depth_fav < config.min_book_depth_usd:
                    logger.info(
                        f"DEPTH FILTER: {fav_side} @ {fav_bid:.4f} threshold met "
                        f"but depth=${depth_fav:.0f} < min ${config.min_book_depth_usd:.0f} "
                        f"| opp depth=${depth_opp:.0f} — skipping entry"
                    )
                else:
                    logger.info(
                        f"*** ENTRY SIGNAL *** {fav_side} @ {fav_bid:.4f} >= "
                        f"{config.entry_threshold} | depth=${depth_fav:.0f} (min=${config.min_book_depth_usd:.0f})"
                    )
                    sig = OrderBookSignal(
                        favored_side=fav_side,
                        favored_token_id=fav_tid,
                        best_bid=fav_bid,
                        opposing_best_bid=opp_bid,
                        book_depth_favored=depth_fav,
                        book_depth_opposing=depth_opp,
                        is_entry_worthy=True,
                        up_best_bid=up_bid,
                        down_best_bid=down_bid,
                    )
                    success = await engine.enter(market, sig, config)
                    if success:
                        logger.info("Entry successful -- switching to stop-loss monitor")
                        if notifier:
                            await notifier.notify_market_link(market, context="entered position")
                        result = await run_stop_loss_monitor(
                            engine, market, session, clob_client, config,
                            logger, shutdown_event,
                            notifier=notifier,
                        )
                        logger.info(f"Stop-loss monitor exited: {result}")
                        return
                    else:
                        logger.error("Entry failed -- will retry next tick")

        await _interruptible_sleep(HOT_POLL_INTERVAL, shutdown_event)


async def main_loop(config: Config, logger: logging.Logger) -> None:
    shutdown_event = asyncio.Event()
    notifier = SlackNotifier(
        config.slack_webhook_url, config.slack_tick_webhook_url,
        logger, config.dry_run,
    )

    def _signal_handler():
        logger.info("Shutdown signal received")
        shutdown_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            signal.signal(sig, lambda s, f: _signal_handler())

    if config.dry_run:
        engine = PaperTradeEngine(logger, notifier)
        clob_client = None
        logger.info("=== PAPER TRADING MODE ===")
    else:
        engine = LiveTradeEngine(config, logger, notifier)
        clob_client = await engine._get_client()
        logger.info("=== LIVE TRADING MODE ===")

    if notifier.enabled:
        logger.info("Slack notifications enabled")
    else:
        logger.info("Slack notifications disabled (no SLACK_WEBHOOK_URL)")

    # Start hourly/24h P/L summary task (sends to Slack when webhook configured)
    summary_task = asyncio.create_task(
        _pnl_summary_loop(
            logger, shutdown_event, notifier=notifier, interval_seconds=3600.0
        ),
    )

    async with aiohttp.ClientSession() as session:
        notifier.set_session(session)
        await notifier.notify_startup(config)

        while not shutdown_event.is_set():
            try:
                await _run_one_cycle(
                    engine, session, clob_client, config, logger,
                    shutdown_event, notifier,
                )
                await notifier.maybe_heartbeat(None, engine)
            except Exception as e:
                logger.exception(f"Unhandled error in cycle: {e}")
                await notifier.notify_error(str(e))
                await _interruptible_sleep(MAIN_LOOP_INTERVAL, shutdown_event)

        await notifier.notify_shutdown(engine)

    summary_task.cancel()
    try:
        await summary_task
    except asyncio.CancelledError:
        pass

    if engine.has_position:
        logger.warning(
            f"Shutting down with open position: "
            f"{engine.position.market_slug} {engine.position.side} "
            f"@ {engine.position.entry_price}"
        )


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------


def main():
    config = Config.from_env_and_args()
    config.validate()
    logger = setup_logging(config.log_level)

    logger.info("=" * 60)
    logger.info("Polymarket BTC 15m Up/Down Bot starting")
    logger.info(f"Mode: {'PAPER' if config.dry_run else 'LIVE'}")
    logger.info(f"Entry threshold: {config.entry_threshold}")
    logger.info(f"Stop-loss threshold: {config.stop_loss_threshold}")
    logger.info(f"Entry size: ${config.entry_size_usd}")
    logger.info("=" * 60)

    try:
        asyncio.run(main_loop(config, logger))
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt -- exiting")
    finally:
        logger.info("Bot stopped")


if __name__ == "__main__":
    main()
