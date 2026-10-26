"""
Deals scraping Telegram bot
---------------------------
Polls deal sources (RSS feeds + optional HTML scrapers), filters by keywords,
dedupes with SQLite, and posts new deals to a Telegram chat/channel.

Setup:
  pip install requests feedparser beautifulsoup4
  export TELEGRAM_BOT_TOKEN="123456:ABC..."   # from @BotFather
  export TELEGRAM_CHAT_ID="@yourchannel"      # or numeric chat id (bot must be admin in a channel)
  python deals_bot.py
"""

import hashlib
import html
import os
import re
import sqlite3
import time

import feedparser
import requests
from bs4 import BeautifulSoup

# ---------------- Config ----------------
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

POLL_INTERVAL = 600  # seconds between runs
DB_PATH = "seen_deals.db"

# Add your own RSS/Atom feeds here (Reddit supports .rss on most subreddits)
RSS_FEEDS = [
    "https://www.reddit.com/r/deals/new/.rss",
    # "https://example.com/deals/feed",
]

# Optional HTML scrapers: (url, CSS selector for deal links)
HTML_SOURCES = [
    # ("https://example.com/deals", "a.deal-title"),
]

# Only post deals matching ANY of these (empty list = post everything)
INCLUDE_KEYWORDS = []  # e.g. ["laptop", "headphones", "ssd"]
# Skip deals matching ANY of these
EXCLUDE_KEYWORDS = ["expired", "sold out"]

# Optional: only post if title mentions a discount >= this percent (0 = off)
MIN_DISCOUNT_PERCENT = 0

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; DealsBot/1.0)"}
MAX_POSTS_PER_RUN = 15  # avoids flooding on first run


# ---------------- Storage ----------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY, ts REAL)")
    conn.commit()
    return conn


def is_new(conn, key):
    h = hashlib.sha256(key.encode()).hexdigest()
    return conn.execute("SELECT 1 FROM seen WHERE id=?", (h,)).fetchone() is None


def mark_seen(conn, key):
    h = hashlib.sha256(key.encode()).hexdigest()
    conn.execute("INSERT OR IGNORE INTO seen VALUES (?, ?)", (h, time.time()))
    conn.commit()


# ---------------- Sources ----------------
def fetch_rss(url):
    feed = feedparser.parse(url, request_headers=HEADERS)
    return [
        {"title": e.get("title", "").strip(), "link": e.get("link", "")}
        for e in feed.entries
    ]


def fetch_html(url, selector):
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    deals = []
    for a in soup.select(selector):
        href = a.get("href")
        if not href:
            continue
        if href.startswith("/"):
            href = requests.compat.urljoin(url, href)
        deals.append({"title": a.get_text(strip=True), "link": href})
    return deals


# ---------------- Filtering ----------------
def passes_filters(deal):
    title = deal["title"].lower()
    if INCLUDE_KEYWORDS and not any(k.lower() in title for k in INCLUDE_KEYWORDS):
        return False
    if any(k.lower() in title for k in EXCLUDE_KEYWORDS):
        return False
    if MIN_DISCOUNT_PERCENT:
        m = re.search(r"(\d{1,2})\s*%", title)
        if not m or int(m.group(1)) < MIN_DISCOUNT_PERCENT:
            return False
    return True


# ---------------- Telegram ----------------
def send_deal(deal):
    text = f"🔥 <b>{html.escape(deal['title'])}</b>\n{html.escape(deal['link'])}"
    r = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=20,
    )
    if r.status_code == 429:  # rate limited
        time.sleep(r.json().get("parameters", {}).get("retry_after", 5))
        return send_deal(deal)
    r.raise_for_status()


# ---------------- Main loop ----------------
def run_once(conn):
    deals = []
    for url in RSS_FEEDS:
        try:
            deals += fetch_rss(url)
        except Exception as e:
            print(f"[rss error] {url}: {e}")
    for url, sel in HTML_SOURCES:
        try:
            deals += fetch_html(url, sel)
        except Exception as e:
            print(f"[html error] {url}: {e}")

    posted = 0
    for d in deals:
        if not d["title"] or not d["link"]:
            continue
        if not is_new(conn, d["link"]):
            continue
        mark_seen(conn, d["link"])  # mark even if filtered, so we don't re-check
        if not passes_filters(d):
            continue
        try:
            send_deal(d)
            posted += 1
            time.sleep(1.5)  # be gentle with Telegram's rate limits
        except Exception as e:
            print(f"[telegram error] {e}")
        if posted >= MAX_POSTS_PER_RUN:
            break
    print(f"Run complete: {len(deals)} fetched, {posted} posted")


if __name__ == "__main__":
    conn = init_db()
    if os.environ.get("RUN_ONCE"):  # used by GitHub Actions: run a single pass and exit
        run_once(conn)
        raise SystemExit(0)
    while True:
        run_once(conn)
        time.sleep(POLL_INTERVAL)
