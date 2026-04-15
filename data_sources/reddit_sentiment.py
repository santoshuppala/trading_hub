"""
Reddit sentiment source — r/wallstreetbets and r/stocks.

Requires: REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET
  Create app at: https://www.reddit.com/prefs/apps (select "script" type)
  Set REDDIT_USERNAME and REDDIT_PASSWORD for script-type auth.

Provides:
  - Ticker mention frequency (is AAPL trending on WSB?)
  - Sentiment from post titles/comments (bullish/bearish keywords)
  - Meme stock early warning (sudden mention spike)

Usage:
    from data_sources.reddit_sentiment import RedditSentimentSource
    reddit = RedditSentimentSource()
    mentions = reddit.get_mentions('AAPL')
"""
import logging
import os
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional
from dataclasses import dataclass

import requests

log = logging.getLogger(__name__)

_CACHE_TTL = 300  # 5 minutes

# Subreddits to monitor
_SUBREDDITS = ['wallstreetbets', 'stocks', 'options', 'investing']

# Ticker pattern: $AAPL or standalone AAPL (2-5 uppercase letters)
_TICKER_PATTERN = re.compile(r'\$([A-Z]{2,5})\b|\b([A-Z]{2,5})\b')

# Common words to exclude from ticker detection
_EXCLUDE_WORDS = frozenset({
    'THE', 'AND', 'FOR', 'ARE', 'BUT', 'NOT', 'YOU', 'ALL', 'CAN',
    'HER', 'WAS', 'ONE', 'OUR', 'OUT', 'HAS', 'HIS', 'HOW', 'MAN',
    'NEW', 'NOW', 'OLD', 'SEE', 'WAY', 'MAY', 'DAY', 'TOO', 'ANY',
    'WHO', 'BOY', 'DID', 'GET', 'HIM', 'LET', 'SAY', 'SHE', 'USE',
    'JUST', 'WILL', 'BEEN', 'HAVE', 'THIS', 'THAT', 'WHAT', 'WITH',
    'FROM', 'THEY', 'BEEN', 'SAID', 'EACH', 'THEM', 'THAN', 'WHEN',
    'SOME', 'VERY', 'YOLO', 'HODL', 'MOON', 'BEAR', 'BULL', 'CALL',
    'PUTS', 'HOLD', 'SELL', 'PUMP', 'DUMP', 'EDIT', 'TLDR', 'POST',
    'LIKE', 'LOOK', 'GOOD', 'LONG', 'NEXT', 'BEST', 'LOSS', 'GAIN',
    'HIGH', 'CASH', 'WEEK', 'YEAR', 'LAST', 'BACK', 'DOWN', 'MUCH',
    'EVEN', 'OVER', 'TAKE', 'ONLY', 'COME', 'MAKE', 'KNOW', 'PLAY',
    'FREE', 'MADE', 'KEEP', 'MOVE', 'HELP', 'SAME', 'RISK', 'DEBT',
    'FUND', 'BOND', 'REAL', 'DONT', 'WANT', 'NEED', 'HUGE', 'DEEP',
    'MANY', 'MORE', 'MOST', 'JUST', 'INTO', 'ALSO', 'WELL', 'THEN',
    'IMO', 'LOL', 'CEO', 'CFO', 'IPO', 'ATH', 'ATL', 'FDA', 'SEC',
    'ETF', 'EPS', 'GDP', 'CPI', 'FED', 'RSI', 'USA',
})

_BULLISH_WORDS = frozenset({
    'buy', 'calls', 'moon', 'rocket', 'bullish', 'squeeze', 'tendies',
    'diamond', 'hands', 'yolo', 'long', 'rally', 'breakout', 'surge',
    'pump', 'undervalued', 'cheap', 'dip', 'buying', 'loaded', 'green',
})

_BEARISH_WORDS = frozenset({
    'sell', 'puts', 'crash', 'dump', 'bearish', 'short', 'overvalued',
    'bubble', 'bag', 'holder', 'loss', 'red', 'dead', 'drill', 'tank',
    'plunge', 'dropping', 'selling', 'warning', 'scam', 'fraud',
})


@dataclass
class RedditMentions:
    ticker: str
    mention_count: int           # total mentions across posts
    post_count: int              # number of unique posts mentioning
    bullish_pct: float           # 0.0-1.0
    bearish_pct: float           # 0.0-1.0
    subreddits: Dict[str, int]   # {subreddit: count}
    top_post_title: str          # most upvoted post title mentioning ticker
    mention_velocity: float      # mentions per hour
    is_trending: bool            # mention_count > 2x normal


class RedditSentimentSource:
    """Reddit sentiment from r/wallstreetbets, r/stocks, r/options."""

    def __init__(self, client_id: str = None, client_secret: str = None,
                 username: str = None, password: str = None):
        self._client_id = client_id or os.getenv('REDDIT_CLIENT_ID', '')
        self._client_secret = client_secret or os.getenv('REDDIT_CLIENT_SECRET', '')
        self._username = username or os.getenv('REDDIT_USERNAME', '')
        self._password = password or os.getenv('REDDIT_PASSWORD', '')
        self._session = requests.Session()
        self._session.headers.update({'User-Agent': 'TradingHub/1.0'})
        self._token: Optional[str] = None
        self._token_expiry: float = 0
        self._cache: Dict[str, tuple] = {}

        if not self._client_id:
            log.info("[Reddit] No credentials — sentiment unavailable. "
                     "Create app at: https://www.reddit.com/prefs/apps")

    def _get_token(self) -> Optional[str]:
        """Get OAuth2 access token."""
        if self._token and time.time() < self._token_expiry:
            return self._token

        if not self._client_id or not self._client_secret:
            return None

        try:
            resp = requests.post(
                'https://www.reddit.com/api/v1/access_token',
                auth=(self._client_id, self._client_secret),
                data={
                    'grant_type': 'password',
                    'username': self._username,
                    'password': self._password,
                } if self._username else {'grant_type': 'client_credentials'},
                headers={'User-Agent': 'TradingHub/1.0'},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                self._token = data.get('access_token')
                self._token_expiry = time.time() + data.get('expires_in', 3600) - 60
                return self._token
        except Exception as exc:
            log.warning("[Reddit] Auth failed: %s", exc)
        return None

    def _fetch_posts(self, subreddit: str, sort: str = 'hot', limit: int = 25) -> list:
        """Fetch posts from a subreddit."""
        token = self._get_token()
        if not token:
            return []

        try:
            resp = self._session.get(
                f'https://oauth.reddit.com/r/{subreddit}/{sort}',
                headers={'Authorization': f'Bearer {token}', 'User-Agent': 'TradingHub/1.0'},
                params={'limit': limit},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                return [child['data'] for child in data.get('data', {}).get('children', [])]
        except Exception as exc:
            log.debug("[Reddit] Fetch %s/%s failed: %s", subreddit, sort, exc)
        return []

    def _extract_tickers(self, text: str) -> List[str]:
        """Extract stock tickers from text."""
        matches = _TICKER_PATTERN.findall(text)
        tickers = []
        for dollar_match, bare_match in matches:
            ticker = dollar_match or bare_match
            if ticker and ticker not in _EXCLUDE_WORDS and len(ticker) >= 2:
                tickers.append(ticker)
        return tickers

    def _analyze_sentiment(self, text: str) -> tuple:
        """Analyze bullish/bearish sentiment from text. Returns (bullish, bearish)."""
        words = set(text.lower().split())
        bull = len(words & _BULLISH_WORDS)
        bear = len(words & _BEARISH_WORDS)
        return bull, bear

    def get_mentions(self, symbol: str) -> Optional[RedditMentions]:
        """Get mention frequency and sentiment for a ticker across Reddit."""
        cache_key = f'mentions_{symbol}'
        cached = self._cache.get(cache_key)
        if cached and (time.time() - cached[0]) < _CACHE_TTL:
            return cached[1]

        if not self._client_id:
            return None

        mention_count = 0
        post_count = 0
        bullish_total = 0
        bearish_total = 0
        sub_counts: Dict[str, int] = {}
        top_post = ''
        top_score = 0

        for subreddit in _SUBREDDITS:
            posts = self._fetch_posts(subreddit, sort='hot', limit=50)
            for post in posts:
                title = post.get('title', '')
                selftext = post.get('selftext', '')[:500]
                full_text = f"{title} {selftext}"

                tickers_in_post = self._extract_tickers(full_text)
                if symbol in tickers_in_post:
                    count = tickers_in_post.count(symbol)
                    mention_count += count
                    post_count += 1
                    sub_counts[subreddit] = sub_counts.get(subreddit, 0) + 1

                    bull, bear = self._analyze_sentiment(full_text)
                    bullish_total += bull
                    bearish_total += bear

                    score = post.get('score', 0)
                    if score > top_score:
                        top_score = score
                        top_post = title[:200]

        total_sentiment = bullish_total + bearish_total
        bullish_pct = bullish_total / total_sentiment if total_sentiment > 0 else 0.5
        bearish_pct = bearish_total / total_sentiment if total_sentiment > 0 else 0.5

        result = RedditMentions(
            ticker=symbol,
            mention_count=mention_count,
            post_count=post_count,
            bullish_pct=round(bullish_pct, 4),
            bearish_pct=round(bearish_pct, 4),
            subreddits=sub_counts,
            top_post_title=top_post,
            mention_velocity=round(mention_count / 4.0, 1),  # ~4 hours of hot posts
            is_trending=mention_count >= 10,
        )

        self._cache[cache_key] = (time.time(), result)
        return result

    def trending_tickers(self, limit: int = 20) -> List[Dict]:
        """Get the most mentioned tickers across all monitored subreddits.

        Returns list sorted by mention count, useful for discovering
        what retail is focused on.
        """
        cache_key = 'trending'
        cached = self._cache.get(cache_key)
        if cached and (time.time() - cached[0]) < _CACHE_TTL:
            return cached[1]

        if not self._client_id:
            return []

        ticker_counts: Counter = Counter()
        ticker_sentiment: Dict[str, Dict] = defaultdict(lambda: {'bull': 0, 'bear': 0})

        for subreddit in _SUBREDDITS:
            posts = self._fetch_posts(subreddit, sort='hot', limit=50)
            for post in posts:
                title = post.get('title', '')
                selftext = post.get('selftext', '')[:500]
                full_text = f"{title} {selftext}"

                tickers = self._extract_tickers(full_text)
                for ticker in set(tickers):  # dedupe per post
                    ticker_counts[ticker] += 1
                    bull, bear = self._analyze_sentiment(full_text)
                    ticker_sentiment[ticker]['bull'] += bull
                    ticker_sentiment[ticker]['bear'] += bear

        results = []
        for ticker, count in ticker_counts.most_common(limit):
            sent = ticker_sentiment[ticker]
            total = sent['bull'] + sent['bear']
            results.append({
                'ticker': ticker,
                'mentions': count,
                'bullish_pct': round(sent['bull'] / total, 2) if total > 0 else 0.5,
                'bearish_pct': round(sent['bear'] / total, 2) if total > 0 else 0.5,
            })

        self._cache[cache_key] = (time.time(), results)
        log.info("[Reddit] Trending: %s", ', '.join(f"{r['ticker']}({r['mentions']})" for r in results[:5]))
        return results
