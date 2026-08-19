"""The RSS feed registry.

Adding a source is a one-line change here. Categories are free-form tags
carried through to the queue so downstream consumers can filter/segment later.

NOTE: RSS endpoints change over time — verify these resolve before a long run
(`curl -sI <url>`). Curate toward whatever Polymarket actually has markets on:
politics, crypto, finance, sports, world events.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Feed:
    name: str
    url: str
    category: str


FEEDS: list[Feed] = [
    # World / general
    Feed("BBC World", "http://feeds.bbci.co.uk/news/world/rss.xml", "world"),
    Feed("Guardian World", "https://www.theguardian.com/world/rss", "world"),
    Feed("NPR News", "https://feeds.npr.org/1001/rss.xml", "world"),
    Feed("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml", "world"),
    # Politics
    Feed("Guardian US Politics", "https://www.theguardian.com/us-news/us-politics/rss", "politics"),
    Feed("Politico", "https://rss.politico.com/politics-news.xml", "politics"),
    Feed("The Hill", "https://thehill.com/homenews/feed/", "politics"),
    Feed("NPR Politics", "https://feeds.npr.org/1014/rss.xml", "politics"),
    Feed("BBC Politics", "https://feeds.bbci.co.uk/news/politics/rss.xml", "politics"),
    Feed("Guardian Politics", "https://www.theguardian.com/politics/rss", "politics"),
    Feed("ABC News Politics", "https://abcnews.go.com/abcnews/politicsheadlines", "politics"),
    Feed("Foreign Policy", "https://foreignpolicy.com/feed/", "politics"),
    # Crypto
    Feed("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/", "crypto"),
    Feed("Cointelegraph", "https://cointelegraph.com/rss", "crypto"),
    # Finance / business
    Feed("BBC Business", "https://feeds.bbci.co.uk/news/business/rss.xml", "finance"),
    Feed("CNBC Top News", "https://www.cnbc.com/id/100003114/device/rss/rss.html", "finance"),
    # Sports
    Feed("ESPN", "https://www.espn.com/espn/rss/news", "sports"),
]
