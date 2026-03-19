# ============================================================
# NEXUS TRADER — Reddit Fetcher (PRAW)
# ============================================================
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_SUBREDDITS = [
    "CryptoCurrency",
    "Bitcoin",
    "ethereum",
    "CryptoMarkets",
    "altcoin",
]

_USER_AGENT = "NexusTrader/1.0 (crypto sentiment scraper)"


def fetch_reddit_posts(
    client_id: str,
    client_secret: str,
    subreddits: Optional[list[str]] = None,
    limit_per_sub: int = 15,
    sort: str = "hot",          # hot | new | top
    symbol: Optional[str] = None,
) -> list[dict]:
    """
    Fetch Reddit posts via PRAW (read-only, no user auth needed).
    Returns a list of normalised post dicts:
    {
        title, text, url, subreddit, score, num_comments,
        published_at (datetime), symbol, raw
    }
    """
    if not client_id or not client_secret:
        logger.debug("Reddit: no credentials configured")
        return []

    for v in (client_id, client_secret):
        if v == "__vault__":
            logger.debug("Reddit: vault sentinel found, skipping")
            return []

    try:
        import praw
    except ImportError:
        logger.warning("praw not installed; pip install praw")
        return []

    subs = subreddits or _DEFAULT_SUBREDDITS
    if symbol:
        subs = subs + [symbol]  # also check symbol-named sub if it exists

    try:
        reddit = praw.Reddit(
            client_id=client_id,
            client_secret=client_secret,
            user_agent=_USER_AGENT,
            check_for_async=False,
        )
    except Exception as exc:
        logger.warning("Reddit auth failed: %s", exc)
        return []

    posts: list[dict] = []
    for sub_name in subs:
        try:
            subreddit = reddit.subreddit(sub_name)
            getter = getattr(subreddit, sort, subreddit.hot)
            for submission in getter(limit=limit_per_sub):
                try:
                    # Filter by symbol if given
                    if symbol:
                        text_lower = (submission.title + " " + (submission.selftext or "")).lower()
                        if symbol.lower() not in text_lower:
                            continue

                    pub_dt = datetime.fromtimestamp(
                        submission.created_utc, tz=timezone.utc
                    )
                    posts.append({
                        "title":        submission.title,
                        "text":         submission.selftext[:500] if submission.selftext else "",
                        "url":          f"https://reddit.com{submission.permalink}",
                        "subreddit":    sub_name,
                        "score":        submission.score,
                        "num_comments": submission.num_comments,
                        "published_at": pub_dt,
                        "symbol":       symbol or "",
                        "raw":          {
                            "id":    submission.id,
                            "flair": submission.link_flair_text or "",
                        },
                    })
                except Exception:
                    continue
        except Exception as exc:
            logger.debug("Reddit sub %s failed: %s", sub_name, exc)
            continue

    logger.info("Reddit: fetched %d posts from %s", len(posts), subs)
    return posts
