#!/usr/bin/env python3
"""
Reading.Sis — Automated podcast digest pipeline.
Runs via GitHub Actions Mon–Fri + Sun at 6 AM Israel time.
Finds new podcast episodes, generates HTML pages, pushes to GitHub Pages,
sends WhatsApp notification to the Reading.Sis group via Green API.
Also maintains a public library index.html and tracks traffic via GoatCounter
(pageviews + save/share/click events). Run with `--library` to rebuild the index.
"""

from __future__ import annotations

import base64
import datetime
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any

import feedparser
import requests
from anthropic import Anthropic
from youtube_transcript_api import YouTubeTranscriptApi

# ══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════════════════

GH_PAT           = os.environ["GH_PAT"]
ANTHROPIC_KEY    = os.environ["ANTHROPIC_API_KEY"]
GREENAPI_ID      = os.environ.get("GREENAPI_INSTANCE_ID", "")
GREENAPI_TOKEN   = os.environ.get("GREENAPI_API_TOKEN", "")
GREENAPI_GROUP   = os.environ.get("GREENAPI_GROUP_ID", "")
# Noam's own number — QA failures DM him privately via the bot (not the group).
ALERT_TO_NOAM    = os.environ.get("WHATSAPP_TO_NOAM", "")

MODEL = "claude-sonnet-4-6"        # daily generation + Sis messages
QA_MODEL = "claude-sonnet-4-6"     # QA content review
HAIKU = "claude-haiku-4-5"         # backfill generation (cheap, bulk)
# GoatCounter analytics. Just the site code (the "xxxxx" in xxxxx.goatcounter.com).
# Public by nature (it's visible in page source), so a plain env var is fine —
# no secret needed. When unset, no tracking is injected and pages still work.
GOATCOUNTER_CODE = os.environ.get("GOATCOUNTER_CODE", "")

REPO       = "readingsis/reading-sis"
PAGES_BASE = "https://readingsis.github.io/reading-sis"
GH_API     = "https://api.github.com"
GH_HEADERS = {
    "Authorization": f"token {GH_PAT}",
    "Accept": "application/vnd.github.v3+json",
}

# Podcasts to monitor. Update RSS URLs here if feeds move.
# Order matters: the library's green→gold show-color ramp is computed by each
# show's index here (see show_color), so reordering re-spaces the ramp.
# `chip` = the initials shown on the library's color square.
PODCASTS = [
    # ── English shows (indices 0–13) — alphabetical A→Z ──────────────────────
    {
        "name": "All-In",
        "slug": "all-in",
        "chip": "AI",
        "rss": "https://rss.libsyn.com/shows/254861/destinations/1928300.xml",
        "spotify_show": "",
        "lex_filter": False,
        "genre": "tech",
        "lang": "en",
        "description": "Four tech investors debate business, politics, and the future with the candor you'd only hear in a private room.",
    },
    {
        "name": "BigDeal",
        "slug": "bigdeal",
        "chip": "BD",
        "rss": "https://feeds.megaphone.fm/bigdeal",
        "spotify_show": "",
        "lex_filter": False,
        "genre": "business",
        "lang": "en",
        "description": "Codie Sanchez on buying boring businesses, building wealth outside Wall Street, and the entrepreneurial mindset most people overlook.",
    },
    {
        "name": "Call Her Daddy",
        "slug": "call-her-daddy",
        "chip": "CHD",
        "rss": "https://feeds.simplecast.com/mKn_QmLS",
        "spotify_show": "",
        "lex_filter": False,
        "genre": "entertainment",
        "lang": "en",
        "description": "Alex Cooper's unfiltered conversations on relationships, sex, and modern life — the show your group chat actually talks about.",
    },
    {
        "name": "Conan O'Brien Needs A Friend",
        "slug": "conan",
        "chip": "CB",
        "rss": "https://feeds.simplecast.com/dHoohVNH",
        "spotify_show": "",
        "lex_filter": False,
        "genre": "entertainment",
        "lang": "en",
        "description": "Conan's delusional quest for friendship with the world's most famous people, one awkward conversation at a time.",
    },
    {
        "name": "Crime Junkie",
        "slug": "crime-junkie",
        "chip": "CJ",
        "rss": "https://feeds.simplecast.com/qm_9xx0g",
        "spotify_show": "",
        "lex_filter": False,
        "show_format": "true_crime",
        "genre": "storytelling",
        "lang": "en",
        "description": "Straightforward, addictive true crime delivered weekly — no fluff, just the case.",
    },
    {
        "name": "The Diary Of A CEO",
        "slug": "doac",
        "chip": "DOAC",
        "rss": "https://rss2.flightcast.com/xmsftuzjjykcmqwolaqn6mdn",
        "spotify_show": "",
        "lex_filter": False,
        "genre": "business",
        "lang": "en",
        # Skip the Friday "Most Replayed Moment" clip episodes — they're short
        # recaps of older episodes, not new full episodes.
        "skip_title_re": r"most replayed|moment[s]?:|highlight",
        "description": "Raw, long-form conversations with the world's most successful entrepreneurs on what it really takes to build something great.",
    },
    {
        "name": "Freakonomics Radio",
        "slug": "freakonomics",
        "chip": "FK",
        "rss": "https://feeds.simplecast.com/Y8lFbOT4",
        "spotify_show": "",
        "lex_filter": False,
        "genre": "storytelling",
        "lang": "en",
        "description": "The hidden economics behind everyday decisions — data-driven, counterintuitive, and impossible to predict.",
    },
    {
        "name": "Hard Fork",
        "slug": "hard-fork",
        "chip": "HF",
        "rss": "https://feeds.simplecast.com/l2i9YnTd",
        "spotify_show": "https://open.spotify.com/show/44fllCS2FTFr2x1ouYggDj",
        "lex_filter": False,
        "genre": "tech",
        "lang": "en",
        "description": "The New York Times technology journalists making sense of an industry that's constantly breaking.",
    },
    {
        "name": "Lenny's Podcast",
        "slug": "lennys",
        "chip": "LP",
        "rss": "https://api.substack.com/feed/podcast/10845.rss",
        "spotify_show": "https://open.spotify.com/show/2dR1MUZEHCOnz1LVfNac0j",
        "lex_filter": False,
        "genre": "tech",
        "lang": "en",
        "description": "Deep-dive conversations on product, growth, and career with PMs and founders from the world's top companies.",
    },
    {
        "name": "Lex Fridman Podcast",
        "slug": "lex-fridman",
        "chip": "LX",
        "rss": "https://lexfridman.com/feed/podcast/",
        "spotify_show": "",
        "lex_filter": True,  # Only tech/AI/science/business guests
        "genre": "tech",
        "lang": "en",
        "description": "The scientists and engineers reshaping AI, physics, and technology — in their own words.",
    },
    {
        "name": "Pivot",
        "slug": "pivot",
        "chip": "PV",
        "rss": "https://feeds.megaphone.fm/pivot",
        "spotify_show": "https://open.spotify.com/show/6UNmc4j2KaJTDr4gKXqYci",
        "lex_filter": False,
        "genre": "tech",
        "lang": "en",
        "description": "Kara Swisher and Scott Galloway break down the week's biggest tech and business stories with sharp takes and zero filter.",
    },
    {
        "name": "SmartLess",
        "slug": "smartless",
        "chip": "SL",
        "rss": "https://feeds.simplecast.com/hNaFxXpO",
        "spotify_show": "",
        "lex_filter": False,
        "show_format": "panel",
        "genre": "entertainment",
        "lang": "en",
        "description": "Jason Bateman, Sean Hayes, and Will Arnett interview a surprise guest each week — funny, warm, and genuinely unpredictable.",
    },
    {
        "name": "Startup for Startup",
        "slug": "startup-for-startup",
        "chip": "SS",
        "rss": "https://omny.fm/shows/startupforstartup/playlists/podcast.rss",
        "spotify_show": "",
        "lex_filter": False,
        "genre": "business",
        "lang": "he",
        "hold": False,
        "backfill_since": "2026-06-01",
        "description": "האתגרים האמיתיים של בנייה — גיוס, מוצר ותרבות ארגונית מבפנים, מ-monday.com.",
    },
    {
        "name": "This Past Weekend w/ Theo Von",
        "slug": "theo-von",
        "chip": "TV",
        "rss": "https://feeds.megaphone.fm/thispastweekend",
        "spotify_show": "",
        "lex_filter": False,
        "genre": "entertainment",
        "lang": "en",
        "description": "Theo Von's curious, wandering conversations with comedians, athletes, and people who've lived unusual lives.",
    },
    # ── Hebrew shows (indices 14–19) — Hebrew alphabetical א→ת ───────────────
    {
        "name": "אחד ביום",
        "slug": "echad-beyom",
        "chip": "אב",
        "rss": "https://omny.fm/shows/ehadbeyom/playlists/ehadbeyom.rss",
        "spotify_show": "",
        "lex_filter": False,
        "genre": "storytelling",
        "lang": "he",
        "hold": False,
        "backfill_since": "2026-06-15",
        "description": "סיפור אחד, נבחר, מוסבר לעומק — בכל יום עם אלעד שמחיוף.",
    },
    {
        "name": "גיקונומי",
        "slug": "geekonomy",
        "chip": "גק",
        "rss": "https://feed.podbean.com/geekonomy/feed.xml",
        "spotify_show": "",
        "lex_filter": False,
        "genre": "business",
        "lang": "he",
        "hold": False,
        "backfill_since": "2026-06-01",
        "description": "שיחות רחבות אופק על טכנולוגיה, עסקים, פוליטיקה ותרבות — מעל 1,100 פרקים.",
    },
    {
        "name": "חיות כיס",
        "slug": "hayot-kis",
        "chip": "חכ",
        "rss": "https://omny.fm/shows/hayot-kiss/playlists/podcast.rss",
        "spotify_show": "",
        "lex_filter": False,
        "genre": "business",
        "lang": "he",
        "hold": False,
        "backfill_since": "2026-06-01",
        "description": "כלכלה בשפת בני אדם — סיפורים אנושיים שמאחורי הכוחות הכלכליים הגדולים. פודקאסט הכלכלה של כאן.",
    },
    {
        "name": "חצי שעה של השראה",
        "slug": "chatzi-shaa",
        "chip": "חש",
        "rss": "https://feeds.soundcloud.com/users/soundcloud:users:313037130/sounds.rss",
        "spotify_show": "",
        "lex_filter": False,
        "genre": "business",
        "lang": "he",
        "hold": False,
        "backfill_since": "2026-06-01",
        "description": 'ראיונות חודשיים עם מנכ"לים ויזמים ישראלים על חדשנות, תרבות ארגונית והדרך שלהם.',
    },
    {
        "name": "טראשטק",
        "slug": "trashtech",
        "chip": "טר",
        "rss": "https://anchor.fm/s/f4876104/podcast/rss",
        "spotify_show": "https://open.spotify.com/show/0nGv2IY8OATmjMtkJ7eLHG",
        "lex_filter": False,
        "genre": "tech",
        "lang": "he",
        "hold": False,
        "backfill_since": "2026-06-01",
        "description": "חדשות טק ישראליות ועולמיות, בינה מלאכותית וסטארטאפים — עם עמרי ברק ויואב צוקר.",
    },
    {
        "name": "שיר אחד",
        "slug": "shir-echad",
        "chip": "שא",
        "rss": "https://omny.fm/shows/one-song/playlists/podcast.rss",
        "spotify_show": "",
        "lex_filter": False,
        "show_format": "music",
        "genre": "storytelling",
        "lang": "he",
        "hold": False,
        "backfill_since": "2026-06-15",
        "description": "כל פרק מפרק שיר אחד — ההיסטוריה שלו, המשמעות, והדרך שהפך לסמל תרבותי.",
    },
]


# ══════════════════════════════════════════════════════════════════════════════
# SCHEDULING
# ══════════════════════════════════════════════════════════════════════════════

def now_israel() -> datetime.datetime:
    """Current datetime in Israel time (UTC+3, approximation)."""
    return datetime.datetime.utcnow() + datetime.timedelta(hours=3)


def get_schedule() -> datetime.timedelta:
    """Search window for new episodes. The pipeline now runs EVERY day — no
    weekend hold, no Sunday flush — so each episode goes out the morning after
    it drops. The 36h overlap absorbs late crons / missed runs; the tracker
    dedupes anything already handled, so overlap is safe."""
    return datetime.timedelta(hours=36)


# ══════════════════════════════════════════════════════════════════════════════
# GITHUB HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def gh_get(path: str) -> dict:
    r = requests.get(f"{GH_API}/repos/{REPO}/contents/{path}", headers=GH_HEADERS)
    r.raise_for_status()
    return r.json()


def gh_put(path: str, content: bytes, message: str, sha: str | None = None) -> dict:
    body: dict[str, Any] = {
        "message": message,
        "content": base64.b64encode(content).decode(),
    }
    if sha:
        body["sha"] = sha
    r = requests.put(f"{GH_API}/repos/{REPO}/contents/{path}", headers=GH_HEADERS, json=body)
    r.raise_for_status()
    return r.json()


def gh_exists(path: str) -> bool:
    r = requests.get(f"{GH_API}/repos/{REPO}/contents/{path}", headers=GH_HEADERS)
    return r.status_code == 200


def get_tracker() -> tuple[dict, str]:
    try:
        data = gh_get("tracker.json")
        tracker = json.loads(base64.b64decode(data["content"]))
        return tracker, data["sha"]
    except requests.HTTPError:
        return {"processed": [], "queued": []}, ""


def save_tracker(tracker: dict, sha: str) -> str:
    # default=str is a safety net: a stray datetime must never crash the save
    # and abort the whole run (datetimes round-trip fine as ISO-ish strings).
    content = json.dumps(tracker, indent=2, ensure_ascii=False, default=str).encode()
    resp = gh_put("tracker.json", content, "chore: update tracker", sha or None)
    return resp.get("content", {}).get("sha", "")


# ══════════════════════════════════════════════════════════════════════════════
# EPISODE DISCOVERY (RSS)
# ══════════════════════════════════════════════════════════════════════════════

# General rerun/repeat filter — applies to ALL shows, not just one. Catches
# flashback/rerelease episodes before any generation or QA cost is spent.
# Per-show filters (e.g. DOAC's "most replayed") are separate and stay in
# addition to this, since they catch show-specific patterns this can't.
RERUN_TITLE_RE = r"\bFBF\b|flashback friday|re-release|rerun|replay|best of|throwback|encore"

# Short/bonus episode skip — applies to ALL shows. A short RSS duration with no
# verified video match is almost always a solo/bonus segment, not a flagship
# episode (the video search just hasn't failed to find something real — there's
# nothing to find). Can only be checked after video search runs, unlike the
# title filters above which skip before any search cost.
SHORT_EPISODE_THRESHOLD_SEC = 25 * 60


def fetch_new_episodes(
    podcast: dict,
    cutoff: datetime.datetime,
    processed_ids: set,
    queued_ids: set,
) -> list[dict]:
    """Parse podcast RSS feed, return episodes published after cutoff that aren't already tracked."""
    try:
        feed = feedparser.parse(podcast["rss"])
    except Exception as e:
        print(f"  RSS error: {e}")
        return []

    skip_re = podcast.get("skip_title_re")

    # Pass 1: collect all in-window, distinct episodes (dedupe true duplicates).
    seen_keys: set[str] = set()
    raw: list[tuple[datetime.datetime, Any]] = []
    for entry in feed.entries:
        if not getattr(entry, "published_parsed", None):
            continue
        pub_il = datetime.datetime(*entry.published_parsed[:6]) + datetime.timedelta(hours=3)
        if pub_il < cutoff:
            break  # RSS is newest-first
        if skip_re and re.search(skip_re, entry.title, re.IGNORECASE):
            print(f"  Skipping clip/recap episode: {entry.title[:60]}")
            continue
        if re.search(RERUN_TITLE_RE, entry.title, re.IGNORECASE):
            print(f"  Skipping rerun/flashback episode: {entry.title[:60]}")
            continue
        # Identity = RSS guid (preferred) or title. Some feeds list the same
        # episode twice; that's a true duplicate, not a second episode.
        key = (getattr(entry, "id", None) or entry.title or "").strip()
        if key in seen_keys:
            continue
        seen_keys.add(key)
        raw.append((pub_il, entry))

    # Collision-safe IDs by CHRONOLOGICAL rank within a day, NOT feed order:
    # the earliest episode of a day keeps slug-date, later ones get -2, -3…
    # This is stable — an episode's ID never changes when another same-day
    # episode is published later (which would otherwise shift feed positions).
    by_date: dict[str, list[tuple[datetime.datetime, Any]]] = {}
    for pub_il, entry in raw:
        by_date.setdefault(pub_il.strftime("%Y-%m-%d"), []).append((pub_il, entry))

    episodes = []
    for pub_il, entry in raw:
        date_str = pub_il.strftime("%Y-%m-%d")
        same_day = sorted(by_date[date_str], key=lambda x: x[0])  # earliest first
        rank = [e for _, e in same_day].index(entry)
        base = f"{podcast['slug']}-{date_str}"
        ep_id = base if rank == 0 else f"{base}-{rank + 1}"
        if ep_id in processed_ids or ep_id in queued_ids:
            continue

        episodes.append({
            "id":           ep_id,
            "podcast":      podcast["name"],
            "slug_prefix":  podcast["slug"],
            "title":        entry.title,
            "description":  getattr(entry, "summary", ""),
            "pub_dt":       pub_il,
            "date":         date_str,
            "duration_sec": _parse_duration(getattr(entry, "itunes_duration", None)),
            "spotify_show": podcast["spotify_show"],
            "lex_filter":   podcast["lex_filter"],
            "show_format":  podcast.get("show_format", "interview"),
            "lang":         podcast.get("lang", "en"),
        })

    return episodes


# ══════════════════════════════════════════════════════════════════════════════
# YOUTUBE
# ══════════════════════════════════════════════════════════════════════════════

def _parse_duration(s: Any) -> int | None:
    """'HH:MM:SS' / 'MM:SS' / plain seconds → seconds."""
    if not s:
        return None
    s = str(s).strip()
    if s.isdigit():
        return int(s)
    try:
        parts = [int(p) for p in s.split(":")]
    except ValueError:
        return None
    sec = 0
    for p in parts:
        sec = sec * 60 + p
    return sec


def youtube_meta(video_id: str) -> tuple[int | None, datetime.date | None]:
    """Return (duration_seconds, upload_date) for a YouTube video, or
    (None, None) on any failure."""
    try:
        result = subprocess.run(
            ["yt-dlp", f"https://www.youtube.com/watch?v={video_id}",
             "--print", "%(duration)s|%(upload_date)s", "--no-download", "--quiet"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            return None, None
        dur_s, upload_s = result.stdout.strip().splitlines()[-1].split("|")
        dur = int(dur_s) if dur_s.isdigit() else None
        upload = datetime.datetime.strptime(upload_s, "%Y%m%d").date() if upload_s.isdigit() else None
        return dur, upload
    except Exception as e:
        print(f"  Video metadata error: {e}")
        return None, None


def verify_youtube_match(video_id: str, episode: dict) -> bool:
    """The video must be the SAME episode as the RSS item — verified by
    upload date (within days of the RSS publish date) and duration (when the
    RSS provides one). A lookalike video poisons transcript, quotes, and
    timestamps, which is far worse than shipping without video links.
    (Real case: Design Better search returned a 2014 conference talk by the
    same guest.) Reject on any doubt or metadata failure."""
    dur, upload = youtube_meta(video_id)
    if upload is None:
        print("  Video metadata fetch failed — rejecting video")
        return False
    pub = episode["pub_dt"].date()
    if abs((upload - pub).days) > 5:
        print(f"  Rejecting video {video_id}: uploaded {upload}, episode published {pub}")
        return False
    rss_dur = episode.get("duration_sec")
    if rss_dur and dur:
        diff = dur - rss_dur
        # Video longer than the RSS audio is common and safe (extra intro/outro/
        # ad reads baked into the YouTube cut but trimmed from the podcast feed —
        # seen consistently on Call Her Daddy and Conan, ~10-12% over). A video
        # SHORTER than the RSS duration is more often a clip or wrong episode, so
        # keep that side of the check tight.
        tolerance = rss_dur * (0.15 if diff > 0 else 0.08)
        if abs(diff) > max(180, int(tolerance)):
            print(f"  Rejecting video {video_id}: duration {dur}s vs RSS {rss_dur}s")
            return False
    return True


def find_youtube_id(title: str, podcast_name: str, show_format: str = "") -> str | None:
    """Search YouTube for the episode video ID.

    Tries the YouTube Data API first (multiple query forms), then falls back
    to yt-dlp. For true_crime shows strips prefixes like "MURDERED:" from the
    title since YouTube video titles often omit them.
    """
    query = f"{podcast_name} {title}"

    # True crime episodes often have a type prefix ("MURDERED: Jane Doe") that
    # the YouTube video title omits — build a shorter stripped alternative.
    short_title = re.sub(
        r'^(MURDERED|SOLVED|MISSING|CONSPIRACY|UNKNOWN|ALLEGED|KIDNAPPED|ABDUCTED)\s*:\s*',
        '', title, flags=re.IGNORECASE,
    ).strip() if show_format == "true_crime" else title
    short_query = f"{podcast_name} {short_title}"

    def _api_search(q: str) -> str | None:
        api_key = os.environ.get("YOUTUBE_API_KEY", "")
        if not api_key:
            return None
        try:
            r = requests.get(
                "https://www.googleapis.com/youtube/v3/search",
                params={"part": "id", "q": q, "type": "video",
                        "maxResults": 1, "key": api_key},
                timeout=20,
            )
            r.raise_for_status()
            items = r.json().get("items", [])
            if items:
                return items[0]["id"]["videoId"]
        except Exception as e:
            print(f"  YouTube Data API error: {e}")
        return None

    def _dlp_search(q: str) -> str | None:
        try:
            result = subprocess.run(
                ["yt-dlp", f"ytsearch1:{q}", "--print", "%(id)s", "--no-download", "--quiet"],
                capture_output=True, text=True, timeout=90,
            )
            if result.returncode == 0:
                vid = result.stdout.strip().split("\n")[0].strip()
                if re.match(r"^[A-Za-z0-9_-]{11}$", vid):
                    return vid
        except Exception as e:
            print(f"  YouTube search error: {e}")
        return None

    # 1. Full title via Data API
    vid = _api_search(query)
    if vid:
        return vid
    # 2. Stripped/short title via Data API (different query form may surface the video)
    if short_query != query:
        vid = _api_search(short_query)
        if vid:
            return vid
    # 3. Short query via yt-dlp (simpler query is faster and less likely to time out)
    if short_query != query:
        vid = _dlp_search(short_query)
        if vid:
            return vid
    # 4. Full query via yt-dlp as last resort
    return _dlp_search(query)


def get_transcript(video_id: str, max_words: int = 6000) -> list[dict]:
    """Fetch YouTube auto/manual transcript. Returns list of {t, text} dicts.

    Supports both youtube-transcript-api 1.x (instance .fetch) and the old
    0.6.x static API. Uses a Webshare residential proxy when configured —
    YouTube blocks transcript requests from datacenter IPs.
    """
    try:
        if hasattr(YouTubeTranscriptApi, "get_transcript"):   # 0.6.x
            raw = YouTubeTranscriptApi.get_transcript(video_id)
            segs = [(seg["start"], seg["text"]) for seg in raw]
        else:                                                  # 1.x
            proxy_config = None
            ws_user = os.environ.get("WEBSHARE_PROXY_USERNAME", "")
            ws_pass = os.environ.get("WEBSHARE_PROXY_PASSWORD", "")
            if ws_user and ws_pass:
                from youtube_transcript_api.proxies import WebshareProxyConfig
                proxy_config = WebshareProxyConfig(
                    proxy_username=ws_user, proxy_password=ws_pass)
            api = YouTubeTranscriptApi(proxy_config=proxy_config)
            segs = [(s.start, s.text) for s in api.fetch(video_id)]

        result, words = [], 0
        for start, text in segs:
            result.append({"t": int(start), "text": text.strip()})
            words += len(text.split())
            if words >= max_words:
                break
        return result
    except Exception as e:
        print(f"  Transcript unavailable: {e}")
        return []


# ══════════════════════════════════════════════════════════════════════════════
# CONTENT GENERATION (CLAUDE API)
# ══════════════════════════════════════════════════════════════════════════════

def _extract_json(raw: str) -> str:
    """Pull the JSON object out of a model response that may wrap it in
    markdown fences or surround it with prose. json.loads() reports
    'Expecting value: line 1 column 1' for ANY non-JSON-leading string, so we
    must locate the object rather than assume the response starts with '{'."""
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        return raw[start:end + 1]
    return raw


def generate_content(episode: dict, transcript: list[dict], video_id: str,
                     model: str = MODEL,
                     qa_feedback: str | None = None) -> dict | None:
    """Call Claude to generate all page content. Returns structured dict or None.
    `model` lets the backfill use cheaper Haiku; daily pipeline uses MODEL."""
    client = Anthropic(api_key=ANTHROPIC_KEY)

    if transcript:
        transcript_text = "\n".join(f"[{s['t']}s] {s['text']}" for s in transcript)
        source_note = "Full transcript with timestamps (seconds):"
    else:
        transcript_text = episode.get("description", "")
        source_note = "No transcript available. Use RSS description:"

    prompt = f"""You are building a reading digest for Reading.Sis, a service that sends podcast highlights to subscribers via WhatsApp.

Podcast: {episode["podcast"]}
Episode: {episode["title"]}
Published: {episode["date"]}

{source_note}
{transcript_text}

Return a single JSON object (no markdown fences) with exactly these keys:

{{
  "guest": "Full name of guest (or 'Various' for panel shows like Pivot / All-In / Hard Fork)",
  "guest_line": "Short line shown under the title — 'with [Guest Name]' for interviews, host names for panels",
  "bio_section_title": "'About [Guest Name]' for interviews, 'About the show' for panels",
  "bio_text": "3-4 sentences about the guest (interview) OR 2-3 sentences about the show and hosts (panel)",
  "duration_str": "Estimated episode duration e.g. '1h 23m'",
  "read_time": <integer — word count of tldr+moments+takeaways divided by 150, rounded up>,
  "tldr": "2-3 sentences. The core argument or biggest insight. Be specific — not generic.",
  "moments": [
    {{
      "speaker": "Name of speaker",
      "quote": "VERBATIM from transcript. If no transcript available, write a compelling paraphrase based on the description — no prefix or label, just the quote itself.",
      "context": "1 sentence: when or why this was said",
      "timestamp_seconds": <integer>,
      "timestamp_display": "M:SS or H:MM:SS format"
    }}
  ],
  "takeaways": [
    {{
      "headline": "5-8 word bold headline",
      "body": "2-3 sentence explanation for a tech/product professional",
      "insight": <integer 1-10: how non-obvious / surprising vs common knowledge>,
      "actionability": <integer 1-10: how much a listener can actually act on it>,
      "specificity": <integer 1-10: how backed by concrete data, examples, or names>
    }}
  ],
  "whatsapp_teaser": "One punchy sentence capturing the episode's most surprising or useful argument. Starts with guest first name (or show name for panels). This is what gets people to click.",
  "skip": false,
  "skip_reason": ""
}}

Hard rules:
- Provide exactly 5 moments.
- Provide between 3 and 10 takeaways — extract the most DISTINCT, substantive takeaways the episode genuinely contains. Quality over quantity: do NOT pad with filler or split one idea into several, and do NOT force exactly 3. Score every takeaway 1-10 on insight, actionability, and specificity per the schema.
- Verbatim quotes only — never clean up or paraphrase. Keep "um", "like", filler words.
- Only use timestamps that actually appear in the transcript. If uncertain, use 0.
- For Lex Fridman episodes ONLY: keep the episode (skip=false) only if the guest's work is clearly in technology, AI/ML, computing, engineering, hard science (physics/biology/chemistry/math), business, startups, or economics. Set skip=true for everyone else — including historians, explorers, naturalists, musicians, artists, athletes, entertainers, religious figures, pure philosophers, and politicians — and give skip_reason. When in doubt for a Lex episode, skip.
- Return pure JSON. No markdown. No explanation."""

    if episode.get("show_format") == "true_crime":
        podcast_name = episode.get("podcast", "the show")
        prompt += f"""

Show format note: This is a TRUE CRIME / MYSTERY STORY episode — no traditional interview guest.
- "guest": name of the case subject, victim, or main person featured (or "Various" for multi-case episodes)
- "guest_line": "Case: [brief identifier]" — e.g. "Case: Jane Doe" or "Case: The Zodiac Killer"
- "bio_section_title": "About {podcast_name}"
- "bio_text": 2-3 sentences about the show, its hosts, and the kinds of stories it typically covers — NOT about the current episode's case (the TL;DR already covers that)
- "moments": use the host/narrator name as speaker (e.g. "Ashley Flowers", "MrBallen")
- "takeaways": key facts, timeline turns, and revelations — not career/business advice style
- "actionability" scores will naturally be low for true crime; score "insight" and "specificity" higher"""

    if episode.get("show_format") == "panel":
        podcast_name = episode.get("podcast", "the show")
        prompt += f"""

Show format note: This is a PANEL / ENSEMBLE show — the recurring hosts ARE the show's identity.
- "bio_section_title": "About {podcast_name}"
- "bio_text": 2-3 sentences about the show, its regular hosts, and its typical format/style — NOT about the guest of this episode (the TL;DR already covers that)"""

    if episode.get("lang") == "he":
        prompt += """

Language: This is a HEBREW-language podcast. Generate ALL text fields \
(tldr, guest_line, bio_text, takeaway texts, moment quotes, whatsapp_teaser) \
in Hebrew (עברית). Quoted transcript text should remain as-is (already Hebrew). \
Do not translate into English."""

    if episode.get("show_format") == "music":
        prompt += """

Show format note: This is a MUSIC DOCUMENTARY show — each episode analyzes ONE specific song.
- "guest": the song title and artist (e.g., "מחכה לנס — משינה")
- "guest_line": "שיר: [song name] | אמן/ת: [artist name]"
- "bio_section_title": "על השיר"
- "bio_text": 3-4 sentences about the song's story, the artist, and its cultural significance
- Takeaways: insights about lyrics, production choices, cultural moment, or what makes it memorable
- Key moments: discussion points in the episode"""

    if qa_feedback:
        prompt += f"\n\nCORRECTION REQUIRED — your previous attempt was rejected by QA:\n{qa_feedback}\nFix these specific issues in your response."

    try:
        msg = client.messages.create(
            model=model,
            # Up to 20 scored takeaways + 5 moments + bio is well past the old
            # 2500 cap; too low truncates the JSON mid-object ("Expecting ','
            # delimiter") and the whole episode fails to generate.
            max_tokens=8000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text if msg.content else ""
        return json.loads(_extract_json(raw))
    except Exception as e:
        print(f"  Claude error: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# HTML GENERATION
# ══════════════════════════════════════════════════════════════════════════════

# The canonical template. Placeholders use {{UPPER_SNAKE}} convention.
HTML_TEMPLATE = """<!DOCTYPE html>
<html LANG_DIR_ATTRS>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
  <meta name="description" content="TLDR_FIRST_SENTENCE">
  <title>Reading.Sis — EPISODE_TITLE</title>
FAVICON_LINKS
GOATCOUNTER_SCRIPT
HEBREW_FONT_LINK
  <style>
    :root {
      --bg: #111111; --card-bg: #181816; --green: #0EB88A;
      --text-primary: #F0EFE8; --text-secondary: #C8C7C0;
      --text-muted: #999990; --text-dim: #666660; --text-faint: #555550;
      --border: #2A2A28; --divider: #222220; --icon-bg: #222220; --icon-border: #3A3A38;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    html, body { background: var(--bg); color: var(--text-secondary); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; -webkit-font-smoothing: antialiased; }
    .page { max-width: 430px; margin: 0 auto; padding-bottom: 100px; min-height: 100vh; }
    .app-bar { position: sticky; top: 0; z-index: 100; background: var(--bg); border-bottom: 1px solid var(--divider); display: flex; align-items: center; justify-content: space-between; padding: 14px 18px; }
    .logo { display: flex; align-items: center; gap: 8px; text-decoration: none; }
    .logo:active { opacity: 0.6; }
    .logo-text { font-size: 15px; font-weight: 700; color: var(--text-primary); letter-spacing: -0.3px; }
    .logo-text span { color: var(--green); }
    .bookmark-btn { background: none; border: none; cursor: pointer; padding: 4px; color: var(--text-faint); transition: color 0.2s; }
    .bookmark-btn.saved { color: var(--green); }
    .hero { padding: 20px 18px 16px; border-bottom: 1px solid var(--divider); }
    .podcast-badge { display: inline-block; background: rgba(14,184,138,0.12); color: var(--green); font-size: 10px; font-weight: 700; letter-spacing: 0.8px; text-transform: uppercase; padding: 4px 10px; border-radius: 20px; margin-bottom: 12px; }
    .episode-title { font-size: 22px; font-weight: 700; color: var(--text-primary); line-height: 1.3; margin-bottom: 8px; letter-spacing: -0.4px; }
    .guest-name { font-size: 13px; color: var(--text-muted); margin-bottom: 12px; }
    .meta { display: flex; gap: 14px; font-size: 11px; color: var(--text-dim); flex-wrap: wrap; }
    .section { padding: 18px; border-bottom: 1px solid var(--divider); }
    .section-label { font-size: 10px; font-weight: 700; letter-spacing: 1.2px; text-transform: uppercase; color: var(--text-faint); margin-bottom: 10px; }
    .tldr-text { font-size: 14px; line-height: 1.6; color: var(--text-secondary); }
    .moments-section { padding: 18px 0 18px 18px; border-bottom: 1px solid var(--divider); }
    .moments-scroll { display: flex; gap: 10px; overflow-x: auto; padding-right: 18px; padding-bottom: 4px; scrollbar-width: none; }
    .moments-scroll::-webkit-scrollbar { display: none; }
    .moment-card { flex: 0 0 240px; background: var(--card-bg); border: 1px solid var(--border); border-radius: 12px; padding: 14px 14px 40px; position: relative; }
    .moment-speaker { font-size: 10px; font-weight: 700; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.6px; margin-bottom: 8px; }
    .moment-quote { font-size: 13px; font-style: italic; color: var(--text-primary); line-height: 1.5; margin-bottom: 8px; }
    .moment-rm-link { color: var(--text-primary); text-decoration: underline; cursor: pointer; }
    .qsheet-back { position: fixed; inset: 0; z-index: 200; background: rgba(0,0,0,0.55); display: none; align-items: flex-end; justify-content: center; }
    .qsheet-back.show { display: flex; }
    .qsheet { width: 100%; max-width: 430px; background: var(--card-bg); border-top-left-radius: 20px; border-top-right-radius: 20px; border-top: 1px solid var(--border); padding: 10px 22px calc(26px + env(safe-area-inset-bottom)); }
    .qsheet-grip { width: 36px; height: 4px; border-radius: 2px; background: var(--border); margin: 0 auto 18px; }
    .qsheet-speaker { font-size: 10px; font-weight: 700; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.8px; margin-bottom: 12px; }
    .qsheet-quote { font-size: 14px; font-style: italic; color: var(--text-primary); line-height: 1.65; margin-bottom: 14px; }
    .qsheet-ctx { font-size: 12.5px; color: var(--text-muted); line-height: 1.5; margin-bottom: 20px; }
    .qsheet-close { width: 100%; background: var(--icon-bg); border: 1px solid var(--icon-border); color: var(--text-primary); font-size: 14px; font-weight: 600; padding: 13px; border-radius: 11px; cursor: pointer; font-family: inherit; }
    .moment-context { font-size: 11px; color: var(--text-dim); line-height: 1.4; }
    .moment-timestamp { position: absolute; bottom: 14px; right: 14px; font-size: 11px; color: var(--green); text-decoration: none; font-weight: 600; background: rgba(14,184,138,0.1); padding: 3px 8px; border-radius: 6px; }
    .bio-toggle { display: flex; align-items: center; justify-content: space-between; cursor: pointer; user-select: none; }
    .bio-chevron { color: var(--text-faint); font-size: 16px; transition: transform 0.2s; line-height: 1; }
    .bio-chevron.open { transform: rotate(90deg); }
    .bio-content { display: none; margin-top: 12px; font-size: 13px; line-height: 1.6; color: var(--text-secondary); }
    .bio-content.open { display: block; }
    .takeaway { display: flex; gap: 12px; margin-bottom: 16px; }
    .takeaway:last-child { margin-bottom: 0; }
    .takeaway-num { flex: 0 0 22px; height: 22px; border-radius: 50%; background: rgba(14,184,138,0.12); color: var(--green); font-size: 11px; font-weight: 700; display: flex; align-items: center; justify-content: center; margin-top: 2px; }
    .takeaway-text { font-size: 13px; line-height: 1.6; color: var(--text-secondary); }
    .takeaway-text strong { color: var(--text-primary); font-weight: 500; }
    .takeaways-rest[hidden] { display: none; }
    .takeaways-toggle { margin-top: 14px; background: none; border: none; color: var(--green); font-size: 13px; font-weight: 600; cursor: pointer; padding: 4px 0; }
    .bottom-bar { position: fixed; bottom: 0; left: 50%; transform: translateX(-50%); width: 100%; max-width: 430px; background: var(--bg); border-top: 1px solid var(--divider); display: flex; justify-content: space-around; align-items: center; padding: 14px 40px; padding-bottom: calc(14px + env(safe-area-inset-bottom)); }
    .icon-btn { width: 48px; height: 48px; border-radius: 50%; background: var(--icon-bg); border: 1px solid var(--icon-border); display: flex; align-items: center; justify-content: center; color: var(--text-muted); cursor: pointer; text-decoration: none; transition: all 0.15s; }
    .icon-btn:active { opacity: 0.7; }
    .icon-btn.flash { color: var(--green); border-color: var(--green); }
    @media print { .app-bar, .bottom-bar { display: none; } .bio-content { display: block !important; } .page { max-width: 100%; padding-bottom: 0; } .moment-card { break-inside: avoid; } }
  </style>
</head>
<body>
<div class="page">

  <div class="app-bar">
    <a class="logo" href="index.html" aria-label="Back to the library">
      <svg width="26" height="16" viewBox="0 0 26 16" fill="none">
        <circle cx="7.5" cy="8" r="4.5" stroke="#0EB88A" stroke-width="1.5"/>
        <circle cx="18.5" cy="8" r="4.5" stroke="#0EB88A" stroke-width="1.5"/>
        <line x1="12" y1="8" x2="14" y2="8" stroke="#0EB88A" stroke-width="1.5" stroke-linecap="round"/>
        <line x1="3" y1="8" x2="1.5" y2="8" stroke="#0EB88A" stroke-width="1.5" stroke-linecap="round"/>
        <line x1="23" y1="8" x2="24.5" y2="8" stroke="#0EB88A" stroke-width="1.5" stroke-linecap="round"/>
      </svg>
      <span class="logo-text">Reading<span>.Sis</span></span>
    </a>
    <button class="bookmark-btn" id="bookmarkBtn" onclick="handleSave()" aria-label="Save to reading list">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M19 21l-7-5-7 5V5a2 2 0 012-2h10a2 2 0 012 2z"/>
      </svg>
    </button>
  </div>

  <div class="hero">
    <div class="podcast-badge">PODCAST_NAME</div>
    <h1 class="episode-title">EPISODE_TITLE</h1>
    <div class="guest-name">GUEST_LINE</div>
    <div class="meta">
      <span>PUBLISH_DATE_FORMATTED</span>
      <span>READ_TIME min read</span>
      <span>EPISODE_DURATION</span>
    </div>
  </div>

  <div class="section">
    <div class="section-label">TL;DR</div>
    <p class="tldr-text">TLDR</p>
  </div>

  <div class="moments-section">
    <div class="section-label" style="padding-right:18px;">Key Moments</div>
    <div class="moments-scroll">
MOMENTS_HTML
    </div>
  </div>

  <div class="section">
    <div class="bio-toggle" onclick="toggleBio(this)">
      <div class="section-label" style="margin-bottom:0;">BIO_SECTION_TITLE</div>
      <span class="bio-chevron">&#8250;</span>
    </div>
    <div class="bio-content">BIO_TEXT</div>
  </div>

  <div class="section">
    <div class="section-label">Takeaways</div>
TAKEAWAYS_HTML
  </div>

</div>

<div class="bottom-bar">
  <a class="icon-btn" href="YOUTUBE_URL" target="_blank" aria-label="Watch on YouTube" onclick="gcEvent('click-youtube')">
    <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor">
      <path d="M23.495 6.205a3.007 3.007 0 00-2.088-2.088c-1.87-.501-9.396-.501-9.396-.501s-7.507-.01-9.396.501A3.007 3.007 0 00.527 6.205a31.247 31.247 0 00-.522 5.805 31.247 31.247 0 00.522 5.783 3.007 3.007 0 002.088 2.088c1.868.502 9.396.502 9.396.502s7.506 0 9.396-.502a3.007 3.007 0 002.088-2.088 31.247 31.247 0 00.5-5.783 31.247 31.247 0 00-.5-5.805zM9.609 15.601V8.408l6.264 3.602z"/>
    </svg>
  </a>
  <button class="icon-btn" id="shareBtn" onclick="handleShare()" aria-label="Share episode">
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/>
      <line x1="8.59" y1="13.51" x2="15.42" y2="17.49"/>
      <line x1="15.41" y1="6.51" x2="8.59" y2="10.49"/>
    </svg>
  </button>
  <a class="icon-btn" href="SPOTIFY_URL" target="_blank" aria-label="Listen on Spotify" onclick="gcEvent('click-spotify')">
    <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor">
      <path d="M12 0C5.4 0 0 5.4 0 12s5.4 12 12 12 12-5.4 12-12S18.66 0 12 0zm5.521 17.34c-.24.359-.66.48-1.021.24-2.82-1.74-6.36-2.101-10.561-1.141-.418.122-.779-.179-.899-.539-.12-.421.18-.78.54-.9 4.56-1.021 8.52-.6 11.64 1.32.42.18.479.659.301 1.02zm1.44-3.3c-.301.42-.841.6-1.262.3-3.239-1.98-8.159-2.58-11.939-1.38-.479.12-1.02-.12-1.14-.6-.12-.48.12-1.021.6-1.141C9.6 9.9 15 10.561 18.72 12.84c.361.181.54.78.241 1.2zm.12-3.36C15.24 8.4 8.82 8.16 5.16 9.301c-.6.179-1.2-.181-1.38-.721-.18-.601.18-1.2.72-1.381 4.26-1.26 11.28-1.02 15.721 1.621.539.3.719 1.02.419 1.56-.299.421-1.02.599-1.559.3z"/>
    </svg>
  </a>
</div>

<script>
  var pageUrl    = 'PAGE_URL_JS';
  var PAGE_TITLE = 'EPISODE_TITLE_JS — Reading.Sis';
  var EP = {id:'EPISODE_ID_JS', title:'EPISODE_TITLE_JS', show:'EPISODE_SHOW_JS', date:'EPISODE_DATE_JS', url:pageUrl};

  // Fire a GoatCounter custom event (e.g. 'save', 'share', 'click-youtube').
  // Path is scoped to this episode so events break down per page. No-op when
  // GoatCounter isn't loaded (code unset, or blocked).
  function gcEvent(name) {
    if (window.goatcounter && window.goatcounter.count) {
      var ep = (location.pathname.split('/').pop() || 'page').replace(/\.html$/, '');
      window.goatcounter.count({path: ep + '-' + name, title: name, event: true});
    }
  }

  // Save = bookmark to localStorage (anonymous, per-device). The library's
  // "Saved" page reads this same key. Toggles on repeat tap.
  function savedList() { try { return JSON.parse(localStorage.getItem('readingsis_saved') || '[]'); } catch(e) { return []; } }
  function isSaved() { return savedList().some(function(x){ return x.id === EP.id; }); }
  function renderSaveBtn() {
    var btn = document.getElementById('bookmarkBtn');
    if (btn) btn.classList.toggle('saved', isSaved());
  }
  function handleSave() {
    gcEvent('save');
    var list = savedList();
    if (isSaved()) { list = list.filter(function(x){ return x.id !== EP.id; }); }
    else { list.push({id:EP.id, title:EP.title, show:EP.show, date:EP.date, url:EP.url}); }
    localStorage.setItem('readingsis_saved', JSON.stringify(list));
    renderSaveBtn();
  }

  function handleShare() {
    var btn = document.getElementById('shareBtn');
    gcEvent('share');
    if (navigator.share) {
      navigator.share({title: PAGE_TITLE, url: pageUrl}).catch(function() {});
    } else {
      navigator.clipboard && navigator.clipboard.writeText(pageUrl).then(function() {
        btn.classList.add('flash');
        setTimeout(function() { btn.classList.remove('flash'); }, 2000);
      });
    }
  }

  function toggleBio(toggle) {
    var chevron = toggle.querySelector('.bio-chevron');
    var content = toggle.nextElementSibling;
    chevron.classList.toggle('open');
    content.classList.toggle('open');
  }

  function toggleTakeaways() {
    var rest = document.getElementById('takeawaysRest');
    var btn = document.getElementById('takeawaysToggle');
    if (rest.hasAttribute('hidden')) {
      rest.removeAttribute('hidden');
      btn.textContent = 'Show fewer';
    } else {
      rest.setAttribute('hidden', '');
      btn.textContent = 'Show all ' + document.querySelectorAll('.takeaway').length + ' takeaways';
    }
  }

  renderSaveBtn();

  var _QL = Math.round(5 * 13 * 1.5);
  Array.prototype.forEach.call(document.querySelectorAll('.moment-card'), function(card) {
    var q = card.querySelector('.moment-quote');
    if (!q || q.scrollHeight <= _QL + 2) return;
    var sp = (card.querySelector('.moment-speaker') || {}).textContent || '';
    var ctx = (card.querySelector('.moment-context') || {}).textContent || '';
    var full = q.textContent;
    var words = full.split(' '), lo = 0, hi = words.length - 1, mid;
    while (lo < hi) {
      mid = Math.ceil((lo + hi) / 2);
      q.textContent = words.slice(0, mid).join(' ') + '...';
      if (q.scrollHeight <= _QL + 2) lo = mid; else hi = mid - 1;
    }
    q.textContent = '';
    q.appendChild(document.createTextNode(words.slice(0, lo).join(' ') + '... '));
    var rm = document.createElement('span');
    rm.className = 'moment-rm-link';
    rm.textContent = 'Read more';
    (function(s, f, c) { rm.onclick = function() { openQuote(s, f, c); }; })(sp, full, ctx);
    q.appendChild(rm);
  });
  function openQuote(sp, q, ctx) {
    document.getElementById('qsheetSp').textContent = sp;
    document.getElementById('qsheetQ').textContent = q;
    document.getElementById('qsheetCtx').textContent = ctx;
    document.getElementById('qsheetBack').classList.add('show');
  }
  function closeQuote(e) {
    if (e && e.target !== e.currentTarget) return;
    document.getElementById('qsheetBack').classList.remove('show');
  }
</script>
<div class="qsheet-back" id="qsheetBack" onclick="closeQuote(event)"><div class="qsheet"><div class="qsheet-grip"></div><div class="qsheet-speaker" id="qsheetSp"></div><div class="qsheet-quote" id="qsheetQ"></div><div class="qsheet-ctx" id="qsheetCtx"></div><button class="qsheet-close" onclick="closeQuote()">Close</button></div></div>
</body>
</html>"""


def validate_inline_js(html: str) -> bool:
    """Syntax-check every inline <script> with node before publishing.

    A single syntax error (bad escaping, placeholder collision) silently
    kills Save/Share/About on the whole page — refuse to ship that. Skips
    validation when node isn't available rather than blocking the pipeline.
    """
    node = shutil.which("node")
    if not node:
        return True
    for m in re.finditer(r"<script>(.*?)</script>", html, re.S):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write(m.group(1))
            path = f.name
        try:
            r = subprocess.run([node, "--check", path], capture_output=True, text=True, timeout=15)
        finally:
            os.unlink(path)
        if r.returncode != 0:
            print(f"  Inline JS syntax error: {r.stderr.strip()[:300]}")
            return False
    return True


# ══════════════════════════════════════════════════════════════════════════════
# QA STAGE — runs on every page before it ships. Auto-fixes what it can; a page
# only publishes once clean, and its WhatsApp message waits until then.
# ══════════════════════════════════════════════════════════════════════════════

# Distinctive template tokens that must all be consumed by build_html. Chosen to
# never collide with real episode text (unlike e.g. "TLDR" which a guest might say).
PLACEHOLDER_TOKENS = [
    "TLDR_FIRST_SENTENCE", "PUBLISH_DATE_FORMATTED", "BIO_SECTION_TITLE",
    "MOMENTS_HTML", "TAKEAWAYS_HTML", "GOATCOUNTER_SCRIPT", "FAVICON_LINKS",
    "PAGE_URL_JS", "EPISODE_TITLE_JS", "EPISODE_ID_JS", "EPISODE_SHOW_JS",
    "EPISODE_DATE_JS", "YOUTUBE_URL", "SPOTIFY_URL",
    "LANG_DIR_ATTRS", "HEBREW_FONT_LINK",
]

FAVICON_LINKS = (
    '  <link rel="icon" href="favicon.svg" type="image/svg+xml">\n'
    '  <link rel="icon" href="favicon.ico" sizes="any">\n'
    '  <link rel="apple-touch-icon" href="apple-touch-icon.png">'
)


def alert_noam(text: str) -> None:
    """DM Noam privately via the bot (separate from the group)."""
    if not all([GREENAPI_ID, GREENAPI_TOKEN, ALERT_TO_NOAM]):
        return
    chat = re.sub(r"\D", "", ALERT_TO_NOAM) + "@c.us"
    try:
        requests.post(
            f"https://api.green-api.com/waInstance{GREENAPI_ID}/sendMessage/{GREENAPI_TOKEN}",
            json={"chatId": chat, "message": text}, timeout=15,
        )
    except Exception as e:
        print(f"  Alert DM failed: {e}")


def _format_found(found_by_podcast: dict[str, int]) -> str:
    """'1 All-In and 2 Lenny's Podcast' — human list of what RSS turned up."""
    parts = [f"{n} {name}" for name, n in found_by_podcast.items()]
    if len(parts) > 1:
        return ", ".join(parts[:-1]) + " and " + parts[-1]
    return parts[0] if parts else ""


def _run_label(now: datetime.datetime | None = None) -> str:
    """Return 'morning', 'noon', or 'evening' based on the current Israel hour."""
    if now is None:
        now = now_israel()
    h = now.hour
    if h < 10:
        return "morning"
    if h < 16:
        return "noon"
    return "evening"


def _next_send_label(now: datetime.datetime | None = None) -> str:
    """Return the clock time of the next scheduled send slot, e.g. '7:30' or '12:30'."""
    if now is None:
        now = now_israel()
    slots = _remaining_slots_today(now)
    if not slots:
        return "end of day"
    s = slots[0]
    return f"{s.hour}:{s.minute:02d}"


def _send_run_summary(found_by_podcast: dict[str, int], outcomes: list[dict]) -> None:
    """Second morning DM: what the generate phase actually found and prepared,
    with explicit call-outs for anything that failed to generate or got held by
    QA, so a quiet inbox never hides a broken run."""
    published = [o for o in outcomes if o["status"] == "published"]
    held      = [o for o in outcomes if o["status"] == "held"]
    failed    = [o for o in outcomes if o["status"] in ("gen_failed", "push_failed")]
    skipped   = [o for o in outcomes if o["status"] == "skipped"]

    def label(o: dict) -> str:
        guest = (o.get("guest") or "").strip()
        show  = o.get("podcast") or o["id"]
        return f"{show} — {guest}" if guest else (o.get("title") or o["id"])

    # Nothing new at all this run.
    if not found_by_podcast and not published and not held and not failed:
        label = _run_label()
        next_s = _next_send_label()
        alert_noam(f"all done — no new episodes this {label}, so nothing to send at {next_s}. all quiet.")
        return

    lines: list[str] = []
    if found_by_podcast:
        total = sum(found_by_podcast.values())
        lines.append(f"ok — found {total} new episode{'s' if total != 1 else ''}: "
                     f"{_format_found(found_by_podcast)}.")
    if published:
        names = "; ".join(label(o) for o in published)
        next_s = _next_send_label()
        lines.append(f"✅ ready and queued: {names}. they'll go out at {next_s} per the plan.")
    for o in held:
        lines.append(f"⚠️ held by QA, won't send until it's clean — {label(o)}. "
                     f"reason: {o.get('detail') or 'see logs'}.")
    for o in failed:
        lines.append(f"❌ failed to prepare — {label(o)} ({o.get('detail') or 'see logs'}). "
                     f"it'll retry on the next run.")
    if skipped:
        names = "; ".join(label(o) for o in skipped)
        lines.append(f"↩️ skipped by the off-topic filter: {names}.")
    alert_noam("\n".join(lines))


def _fix_timestamps(content: dict, video_duration: int | None, issues: list) -> dict:
    """Drop any moment timestamp that lands past the video's end — a sign of a
    hallucinated or wrong-video timestamp. Setting seconds to 0 makes build_html
    omit the badge rather than link to a bogus position."""
    if not video_duration:
        return content
    for m in content.get("moments", []):
        ts = m.get("timestamp_seconds", 0) or 0
        if ts > video_duration + 5:
            issues.append(("fixed", f"dropped out-of-range timestamp {ts}s (video is {video_duration}s)"))
            m["timestamp_seconds"] = 0
    return content


def qa_content_review(episode: dict, content: dict, transcript: list[dict]) -> dict | None:
    """One Claude call to catch content bugs: fabricated quotes, wrong guest,
    generic TL;DR. Returns a structured verdict, or None if the call fails."""
    transcript_text = "\n".join(s["text"] for s in transcript)[:24000]
    quotes = [{"index": i, "speaker": m.get("speaker", ""), "quote": m.get("quote", "")}
              for i, m in enumerate(content.get("moments", []))]
    hebrew_qa_instructions = """

HEBREW PODCAST — ADDITIONAL CHECKS:
- Verbatim strictness: Hebrew quotes must be EXACTLY as spoken — zero tolerance for paraphrasing, summarizing, or word substitution. Even a single changed word can alter meaning in Hebrew. Treat any non-verbatim quote as fabricated and include it in bad_quote_indexes.
- Speaker attribution: verify every quote is attributed to the correct speaker (host or guest — applies to all). A verbatim quote attributed to the wrong person counts as an error; include it in bad_quote_indexes.
- For co-hosted shows: if the bio/about section describes both hosts, verify their individual descriptions are not swapped.
- Set overall_ok=false for any attribution error, not just fabricated quotes.""" if episode.get("lang") == "he" else ""
    prompt = f"""You are the QA reviewer for Reading.Sis. Check the generated content against the episode transcript and flag problems.

Episode title: {episode["title"]}
Podcast: {episode["podcast"]}
Stated guest: {content.get("guest", "")}
TL;DR: {content.get("tldr", "")}

Quotes used (must be VERBATIM from the transcript):
{json.dumps(quotes, ensure_ascii=False)}

Transcript (may be truncated — only flag a quote if it appears fabricated or materially altered, NOT merely absent from this excerpt):
{transcript_text}

Return a single JSON object, no markdown:
{{
  "guest_ok": true/false,
  "guest_correction": "correct full name, or empty if guest_ok",
  "bad_quote_indexes": [list of indexes whose quote looks fabricated or materially altered],
  "tldr_ok": true/false,
  "overall_ok": true/false,
  "summary": "one short line on what's wrong, or 'clean'"
}}

Set overall_ok=false if the guest is wrong or any quote is fabricated. A generic TL;DR alone (tldr_ok=false) is a warning, not a failure.{hebrew_qa_instructions}"""
    client = Anthropic(api_key=ANTHROPIC_KEY)
    # Retry: the review occasionally returns an empty/non-JSON body. A None
    # here makes qa_episode fail-open (publishes unreviewed), so retry before
    # giving up rather than silently skipping the review.
    for attempt in range(3):
        try:
            # QA runs on Opus (independent from the Sonnet generator) with
            # adaptive thinking — correctness matters more than cost/latency on
            # the last-line content check. max_tokens must leave room for the
            # thinking budget on top of the ~300-token JSON verdict.
            msg = client.messages.create(
                model=QA_MODEL, max_tokens=4000,
                thinking={"type": "adaptive"},
                output_config={"effort": "high"},
                messages=[{"role": "user", "content": prompt}],
            )
            # With thinking on, content[0] is a thinking block — grab the text block.
            raw = next((b.text for b in msg.content if b.type == "text"), "")
            extracted = _extract_json(raw)
            if extracted:
                return json.loads(extracted)
            print(f"  QA review empty response (attempt {attempt + 1})")
        except Exception as e:
            print(f"  QA content review error (attempt {attempt + 1}): {e}")
        time.sleep(2)
    return None


def qa_episode(episode: dict, content: dict, video_id: str | None,
               video_duration: int | None, transcript: list[dict],
               gen_model: str = MODEL, prior_feedback: str | None = None) -> tuple[bool, str, dict, list]:
    """Run the full QA stage on one episode. Auto-fixes timestamps and, on a
    content-review failure, regenerates the content once. Returns
    (passed, html, content, issues). passed=False means real blockers remain
    and the page must not ship yet. The review itself always uses QA_MODEL
    (Opus) for reliability; `gen_model` is used only for regeneration.
    `prior_feedback` carries a failure reason persisted from an EARLIER run
    (cross-run retry) — folded into this run's own regeneration feedback so a
    second-time failure doesn't repeat a mistake already diagnosed before."""
    issues: list = []

    content = _fix_timestamps(content, video_duration, issues)

    # Content review (only meaningful when we have a transcript to check against).
    if transcript:
        review = qa_content_review(episode, content, transcript)
        if review and not review.get("overall_ok", True):
            issues.append(("content", f"review: {review.get('summary', 'content issue')}"))
            feedback_parts = []
            if prior_feedback:
                feedback_parts.append(f"- From a previous attempt: {prior_feedback}")
            feedback_parts.append(f"- {review.get('summary', 'content issue')}")
            if not review.get("guest_ok", True) and review.get("guest_correction"):
                feedback_parts.append(f"- Guest name is WRONG. Correct name: {review['guest_correction']}")
            if review.get("bad_quote_indexes"):
                feedback_parts.append(f"- Quotes at positions {review['bad_quote_indexes']} appear fabricated or altered — use only verbatim transcript text")
            if not review.get("tldr_ok", True):
                feedback_parts.append("- TL;DR is too generic — be specific about what was actually said")
            regen = generate_content(episode, transcript, video_id or "", model=gen_model,
                                     qa_feedback="\n".join(feedback_parts))
            if regen and not regen.get("skip"):
                regen = _fix_timestamps(regen, video_duration, [])
                recheck = qa_content_review(episode, regen, transcript)
                if recheck and recheck.get("overall_ok", True):
                    content = regen
                    issues.append(("fixed", "regenerated content — QA now clean"))
                else:
                    issues.append(("blocker", "content still failing after one regeneration"))
            else:
                issues.append(("blocker", "content regeneration failed"))

    # Structural checks on the rendered page.
    html = build_html(episode, content, video_id or "")
    leftover = [t for t in PLACEHOLDER_TOKENS if t in html]
    if leftover:
        issues.append(("blocker", f"unfilled placeholders: {leftover}"))
    if not validate_inline_js(html):
        issues.append(("blocker", "invalid inline JS"))
    if 'href="#"' in html:
        issues.append(("blocker", 'dead "#" links present'))
    if 'class="logo" href="index.html"' not in html:
        issues.append(("blocker", "logo no longer links back to the library"))

    # Takeaways: we now ask for 3–10 ranked takeaways. Too few is a quality
    # warning (still ships — better than holding); the ranking tolerates missing
    # scores (treated as 0), so don't block on those.
    n_tk = len(content.get("takeaways", []))
    if not (3 <= n_tk <= 10):
        issues.append(("warning", f"takeaways count {n_tk} outside expected 3–10"))

    # Timestamps: if a video was found but every moment timestamp is zero the
    # model either got no transcript or ignored timing data — flag it so the
    # issue is visible in logs / Noam's DMs.
    if video_id:
        moments = content.get("moments", [])
        if moments and all((m.get("timestamp_seconds") or 0) == 0 for m in moments):
            issues.append(("warning", "video found but all moment timestamps are 0 — transcript may be missing or model ignored timing"))

    passed = not any(level == "blocker" for level, _ in issues)
    return passed, html, content, issues


def qa_live_page(page_url: str) -> bool:
    """Final guard in the send phase: re-check the *published* page for valid
    JS and no leftover placeholders before its message goes out."""
    try:
        html = requests.get(page_url, timeout=15).text
    except Exception:
        return False
    if any(t in html for t in PLACEHOLDER_TOKENS):
        return False
    if 'class="logo" href="index.html"' not in html:
        return False
    return validate_inline_js(html)


def _t(s: Any) -> str:
    """Escape HTML special chars for text content (not attributes)."""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def goatcounter_script() -> str:
    """GoatCounter tracking tag, or '' when no site code is configured."""
    if not GOATCOUNTER_CODE:
        return ""
    endpoint = f"https://{GOATCOUNTER_CODE}.goatcounter.com/count"
    return (
        f'  <script data-goatcounter="{endpoint}" '
        f'async src="//gc.zgo.at/count.js"></script>'
    )


def build_html(episode: dict, content: dict, video_id: str) -> str:
    """Populate the HTML template with generated content."""
    # Fall back to search URLs so the YouTube/Spotify buttons always lead
    # somewhere useful ("#" just reloads the page).
    search_q = requests.utils.quote(f"{episode['podcast']} {episode['title']}")
    yt_url = (
        f"https://www.youtube.com/watch?v={video_id}" if video_id
        else f"https://www.youtube.com/results?search_query={search_q}"
    )
    pub_dt: datetime.datetime = episode["pub_dt"]
    page_url = f"{PAGES_BASE}/{episode['id']}.html"
    spotify  = episode.get("spotify_show") or f"https://open.spotify.com/search/{search_q}"

    # Build moments carousel. Timestamp badge only when we have a real video
    # position — a 0:00 badge or a "#" link is worse than no badge.
    # Carousel always runs chronologically (by the quote's position in the
    # episode). Moments without a real timestamp keep their original order, at
    # the end — they can't be placed on the timeline.
    moments_html = ""
    ordered = sorted(
        enumerate(content.get("moments", [])),
        key=lambda im: (im[1].get("timestamp_seconds", 0) or 10**9, im[0]),
    )
    for _, m in ordered:
        ts = m.get("timestamp_seconds", 0) or 0
        ts_html = ""
        if video_id and ts > 0:
            ts_html = (
                f'        <a class="moment-timestamp" href="{yt_url}&t={ts}" target="_blank">'
                f'&#9654; {_t(m["timestamp_display"])}</a>\n'
            )
        moments_html += (
            f'      <div class="moment-card">\n'
            f'        <div class="moment-speaker">{_t(m["speaker"])}</div>\n'
            f'        <div class="moment-quote">&ldquo;{_t(m["quote"])}&rdquo;</div>\n'
            f'        <div class="moment-context">{_t(m["context"])}</div>\n'
            + ts_html +
            f'      </div>\n'
        )

    # Build takeaways: rank by insight + actionability + specificity, show the
    # top 3, hide the rest behind a "Show all" expander.
    def _tk_score(tk: dict) -> int:
        return sum((tk.get(k) or 0) for k in ("insight", "actionability", "specificity"))

    def _tk_html(tk: dict, i: int) -> str:
        headline = tk.get("headline") or tk.get("title") or ""
        body = tk.get("body") or tk.get("text") or tk.get("description") or ""
        return (
            f'    <div class="takeaway">\n'
            f'      <div class="takeaway-num">{i}</div>\n'
            f'      <div class="takeaway-text">'
            f'<strong>{_t(headline)} </strong>{_t(body)}</div>\n'
            f'    </div>\n'
        )

    ranked = sorted(content.get("takeaways", []), key=_tk_score, reverse=True)
    top, rest = ranked[:3], ranked[3:]
    takeaways_html = "".join(_tk_html(tk, i) for i, tk in enumerate(top, 1))
    if rest:
        rest_html = "".join(_tk_html(tk, i) for i, tk in enumerate(rest, len(top) + 1))
        takeaways_html += (
            f'    <div class="takeaways-rest" id="takeawaysRest" hidden>\n{rest_html}    </div>\n'
            f'    <button class="takeaways-toggle" id="takeawaysToggle" '
            f'onclick="toggleTakeaways()">Show all {len(ranked)} takeaways</button>\n'
        )

    tldr      = content.get("tldr", "")
    tldr_first = (tldr.split(".")[0] + ".") if tldr else ""
    read_time = content.get("read_time", 5)
    duration  = content.get("duration_str", "")

    html = HTML_TEMPLATE
    # JS/longer placeholders first: EPISODE_TITLE_JS and PAGE_URL_JS contain
    # EPISODE_TITLE / PAGE_URL as substrings and would be corrupted otherwise.
    def _js(s: str) -> str:
        return str(s).replace("\\", "\\\\").replace("'", "\\'")
    js_title = _js(episode["title"])
    is_he = episode.get("lang") == "he"
    lang_dir = 'lang="he" dir="rtl"' if is_he else 'lang="en"'
    hebrew_font = (
        '<link rel="preconnect" href="https://fonts.googleapis.com">'
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Heebo:wght@300;400;500;700&display=swap">'
        '<style>'
        'html,body{font-family:\'Heebo\',-apple-system,BlinkMacSystemFont,sans-serif;}'
        # Structural elements: force LTR so layout is identical to English pages
        '.app-bar,.nav,.moment-card,.section-label,.takeaway,.bio-toggle{direction:ltr;}'
        # Carousel starts from the right, scrolls left (natural RTL reading order)
        '.moments-scroll{direction:rtl;padding-left:18px;}'
        # Text content: RTL alignment for Hebrew reading
        '.episode-title,.guest-name,.tldr-text,.bio-content,.takeaway-text,.moment-quote,.moment-context,.qsheet-quote,.qsheet-ctx{direction:rtl;text-align:right;}'
        '</style>'
    ) if is_he else ""
    html = html.replace("LANG_DIR_ATTRS",      lang_dir)
    html = html.replace("HEBREW_FONT_LINK",    hebrew_font)
    html = html.replace("EPISODE_TITLE_JS",   js_title)
    html = html.replace("EPISODE_ID_JS",      _js(episode["id"]))
    html = html.replace("EPISODE_SHOW_JS",    _js(episode["podcast"]))
    html = html.replace("EPISODE_DATE_JS",    _js(_fmt_date(episode.get("date", ""), episode.get("lang", "en"))))
    html = html.replace("PAGE_URL_JS",        page_url)
    html = html.replace("TLDR_FIRST_SENTENCE", _t(tldr_first))
    # Text content
    html = html.replace("EPISODE_TITLE",       _t(episode["title"]))
    html = html.replace("PODCAST_NAME",        _t(episode["podcast"]))
    html = html.replace("GUEST_LINE",          _t(content.get("guest_line", "")))
    html = html.replace("PUBLISH_DATE_FORMATTED", _fmt_date(episode.get("date", ""), "he" if is_he else "en"))
    html = html.replace("READ_TIME",           str(read_time))
    html = html.replace("EPISODE_DURATION",    _t(duration))
    html = html.replace("TLDR",                _t(tldr))
    html = html.replace("MOMENTS_HTML",       moments_html)
    html = html.replace("BIO_SECTION_TITLE",  _t(content.get("bio_section_title", "About")))
    html = html.replace("BIO_TEXT",           _t(content.get("bio_text", "")))
    html = html.replace("TAKEAWAYS_HTML",     takeaways_html)
    # URLs (not HTML-escaped)
    html = html.replace("YOUTUBE_URL",        yt_url)
    html = html.replace("SPOTIFY_URL",        spotify)
    html = html.replace("PAGE_URL",           page_url)   # meta tag
    html = html.replace("GOATCOUNTER_SCRIPT", goatcounter_script())
    html = html.replace("FAVICON_LINKS",      FAVICON_LINKS)
    return html


# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC LIBRARY (index.html)
# ══════════════════════════════════════════════════════════════════════════════

# Shareable landing page: every published episode, newest first. No analytics
# numbers are shown here — this link is meant for the WhatsApp group. Traffic is
# tracked silently via the same GoatCounter tag and viewed in GoatCounter's UI.
# ── Color ramp + show metadata ────────────────────────────────────────────────

def _hex_lerp(c1: str, c2: str, t: float) -> str:
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02X%02X%02X" % tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


# Fixed "moderate" gold→green ramp, one stop per show in PODCASTS order
# (Lenny's = gold … Lex = green). Hand-picked so all six read as distinct
# colors. When a show is added, append a stop (and we'll revisit spacing).
_SHOW_RAMP = [
    # English shows (0–13) — alphabetical A→Z
    "#ADBA2F",  # 0   All-In               — yellow-green
    "#D65BA0",  # 1   BigDeal              — pink
    "#CF7E5E",  # 2   Call Her Daddy       — terracotta
    "#5CB8B2",  # 3   Conan                — teal
    "#5BA3D9",  # 4   Crime Junkie         — cornflower
    "#52BD6C",  # 5   The Diary Of A CEO   — green
    "#D4A84B",  # 6   Freakonomics         — amber
    "#83BD4A",  # 7   Hard Fork            — lime
    "#E3B25A",  # 8   Lenny's Podcast      — gold
    "#15B98A",  # 9   Lex Fridman          — emerald
    "#CEB538",  # 10  Pivot                — yellow
    "#A05EC4",  # 11  SmartLess            — purple
    "#9B6FD4",  # 12  Startup for Startup  — lavender
    "#E06A2E",  # 13  Theo Von             — orange
    # Hebrew shows (14–19) — Hebrew alphabetical א→ת
    "#5BC4C4",  # 14  אחד ביום            — cyan
    "#5DC48A",  # 15  גיקונומי             — sage green
    "#D44F6B",  # 16  חיות כיס            — raspberry
    "#7B8AE8",  # 17  חצי שעה של השראה   — periwinkle
    "#5B8AD6",  # 18  טראשטק              — blue
    "#E8C45C",  # 19  שיר אחד             — gold ochre
]


def show_color(index: int, total: int) -> str:
    """The show's fixed color by its index in PODCASTS."""
    if 0 <= index < len(_SHOW_RAMP):
        return _SHOW_RAMP[index]
    return "#15B98A"


def _show_meta() -> dict:
    """name -> {slug, chip, color, index} for every configured podcast."""
    total = len(PODCASTS)
    out = {}
    for i, p in enumerate(PODCASTS):
        chip = p.get("chip") or "".join(w[0] for w in p["name"].split()[:2]).upper()
        out[p["name"]] = {"slug": p["slug"], "chip": chip,
                          "color": show_color(i, total), "index": i,
                          "genre": p.get("genre", ""),
                          "lang": p.get("lang", "en")}
    return out


_HE_MONTHS = ["ינואר","פברואר","מרץ","אפריל","מאי","יוני",
              "יולי","אוגוסט","ספטמבר","אוקטובר","נובמבר","דצמבר"]

def _fmt_date(d: str, lang: str = "en") -> str:
    try:
        dt = datetime.datetime.strptime(d, "%Y-%m-%d")
        if lang == "he":
            return f"{dt.day} ב{_HE_MONTHS[dt.month - 1]} {dt.year}"
        return dt.strftime("%-d %b %Y")
    except (ValueError, TypeError):
        return d or ""


def _library_episodes(tracker: dict) -> tuple[list, dict]:
    """Published episodes enriched with show color/chip/slug, newest first."""
    meta = _show_meta()
    now = now_israel()
    eps = []
    for ep in tracker.get("processed", []):
        if not (isinstance(ep, dict) and ep.get("page_url") and ep.get("title")
                and not ep.get("skipped")):
            continue
        name = ep.get("podcast", "")
        m = meta.get(name)
        if not m:
            slug = re.sub(r"-\d{4}-\d{2}-\d{2}(?:-\d+)?$", "", ep.get("id", "")) or "show"
            m = {"slug": slug, "chip": (name[:2] or "?").upper(),
                 "color": "#15B98A", "index": 99}
        date = ep.get("date") or ""
        # "New" = published within the last true 24 hours. Requires the real
        # publish timestamp (added going forward); episodes processed before
        # this existed just never show the badge — they're all long past "new"
        # by now regardless, so no retroactive backfill of the field needed.
        is_new = False
        published_at = ep.get("published_at")
        if published_at:
            try:
                pub_ts = datetime.datetime.fromisoformat(published_at)
                is_new = (now - pub_ts) <= datetime.timedelta(hours=24)
            except (ValueError, TypeError):
                is_new = False
        eps.append({
            "id": ep["id"], "title": ep["title"], "guest": ep.get("guest") or "",
            "show": name, "slug": m["slug"], "chip": m["chip"], "color": m["color"],
            "date": date, "fdate": _fmt_date(date), "url": f"{ep['id']}.html", "new": is_new,
        })
    eps.sort(key=lambda e: (e["date"], e["id"]), reverse=True)
    return eps, meta


# ── Shared page chrome ────────────────────────────────────────────────────────

LIB_CSS = """
    :root {
      --canvas:#0B0F0E; --surface:#131916; --raised:#1B221E; --line:#2A322C;
      --divider:#1B221E; --tp:#ECF2EE; --tm:#94A39A; --dim:#85958B; --meta:#7C8C83;
      --green:#15B98A; --gold:#E3B25A;
    }
    * { box-sizing:border-box; margin:0; padding:0; }
    html,body { background:var(--canvas); color:var(--tp);
      font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; -webkit-font-smoothing:antialiased; }
    a { text-decoration:none; color:inherit; }
    .page { max-width:430px; margin:0 auto; min-height:100vh; padding-bottom:84px; }
    .hdr { position:sticky; top:0; z-index:50; background:var(--canvas);
      display:grid; grid-template-columns:1fr auto 1fr; align-items:center;
      padding:19px 18px 18px; border-bottom:1px solid var(--divider); }
    .hside { display:flex; align-items:center; }
    .hside.left { justify-self:start; }
    .hside.right { justify-self:end; }
    .hcenter { display:flex; flex-direction:column; align-items:center; gap:2px; }
    .wm { font-size:17px; font-weight:700; color:var(--tp); letter-spacing:-0.3px; }
    .wm span { color:var(--green); }
    .tagline { font-size:11px; color:var(--tm); letter-spacing:0.1px; }
    .account { width:30px; height:30px; border-radius:50%; background:var(--raised);
      border:1px solid var(--line); color:var(--tm); cursor:pointer; padding:0; font:inherit;
      display:flex; align-items:center; justify-content:center; }
    .account:active { opacity:0.7; }
    .account svg { width:16px; height:16px; }
    .sheet-back { position:fixed; inset:0; z-index:100; background:rgba(0,0,0,0.55);
      display:none; align-items:flex-end; justify-content:center; }
    .sheet-back.show { display:flex; }
    .sheet { width:100%; max-width:430px; background:var(--surface);
      border-top-left-radius:20px; border-top-right-radius:20px;
      border-top:1px solid var(--line); padding:10px 22px calc(26px + env(safe-area-inset-bottom)); }
    .sheet-grip { width:36px; height:4px; border-radius:2px; background:var(--line);
      margin:0 auto 18px; }
    .sheet .wm { font-size:18px; display:block; margin-bottom:4px; }
    .sheet .tagline { font-size:12px; color:var(--tm); display:block; margin-bottom:16px; }
    .sheet p { font-size:13.5px; color:var(--tm); line-height:1.6; margin-bottom:12px; }
    .sheet-close { width:100%; margin-top:6px; background:var(--raised); border:1px solid var(--line);
      color:var(--tp); font-size:14px; font-weight:600; padding:12px; border-radius:11px;
      cursor:pointer; font-family:inherit; }
    .backrow { padding:12px 18px 0; }
    .backlink { display:inline-flex; align-items:center; gap:5px; color:var(--tm);
      font-size:13px; font-weight:500; }
    .backlink .bk { font-size:18px; line-height:1; }
    .backlink:active { opacity:0.6; }
    .seclabel { padding:18px 18px 8px; font-size:11px; font-weight:700; letter-spacing:1px;
      text-transform:uppercase; color:var(--dim); }
    .rows { padding:0 18px; }
    .row { display:flex; align-items:center; gap:13px; padding:13px 0;
      border-bottom:1px solid var(--divider); }
    .row:active { opacity:0.65; }
    .chip { flex:0 0 42px; width:42px; height:42px; border-radius:11px; color:#08120D;
      font-size:12px; font-weight:700; letter-spacing:0.2px;
      display:flex; align-items:center; justify-content:center; }
    .row-main { flex:1 1 auto; min-width:0; display:flex; flex-direction:column; gap:3px; }
    .row-title { font-size:14px; font-weight:500; color:var(--tp); line-height:1.35;
      display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }
    .row-meta { font-size:11.5px; color:var(--meta); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
    .newtag { display:inline-block; vertical-align:1px; margin-right:7px; background:rgba(227,178,90,0.16);
      color:var(--gold); font-size:9px; font-weight:700; letter-spacing:0.6px; padding:1px 6px; border-radius:5px; }
    .chev { flex:0 0 auto; color:var(--dim); font-size:20px; line-height:1; }
    .grid { display:grid; grid-template-columns:1fr 1fr; gap:11px; padding:13px 18px 0; }
    .card { display:flex; align-items:center; gap:11px; background:var(--surface);
      border:1px solid var(--line); border-radius:14px; padding:13px; min-height:78px; }
    .card:active { opacity:0.7; }
    .sq { flex:0 0 38px; width:38px; height:38px; border-radius:10px; color:#08120D;
      font-size:11px; font-weight:700; display:flex; align-items:center; justify-content:center; }
    .card-r { display:flex; flex-direction:column; gap:3px; min-width:0; }
    .card-name { font-size:13px; font-weight:500; color:var(--tp); line-height:1.25; }
    .card-meta { display:flex; align-items:center; gap:0; }
    .card-count { font-size:11.5px; color:var(--dim); white-space:nowrap; }
    .lang-en { font-size:10px; padding:1px 5px; border-radius:999px; background:rgba(255,255,255,0.08); color:var(--dim); margin-right:4px; }
    .lang-he { font-size:10px; padding:1px 5px; border-radius:999px; background:rgba(21,185,138,0.15); color:var(--green); margin-right:4px; }
    .genre-tabs { display:flex; gap:6px; flex-wrap:nowrap; overflow-x:auto; padding:0 18px 2px; margin-bottom:12px; scrollbar-width:none; -webkit-overflow-scrolling:touch; }
    .genre-tabs::-webkit-scrollbar { display:none; }
    .genre-tabs::after { content:''; min-width:18px; flex-shrink:0; }
    .gtab { font-size:12px; padding:4px 12px; border-radius:999px; border:0.5px solid var(--dim); background:transparent; color:var(--dim); cursor:pointer; }
    .gtab.active { background:var(--green); border-color:var(--green); color:#fff; }
    .empty { padding:48px 24px; text-align:center; color:var(--dim); font-size:13px; line-height:1.6; }
    .nav { position:fixed; bottom:0; left:50%; transform:translateX(-50%); width:100%; max-width:430px;
      background:var(--canvas); border-top:1px solid var(--divider); display:flex;
      padding:9px 0 calc(9px + env(safe-area-inset-bottom)); }
    .nav-i { flex:1; display:flex; flex-direction:column; align-items:center; gap:3px;
      color:var(--dim); font-size:10px; font-weight:500; }
    .nav-i.on { color:var(--green); }
    .nav-i svg { width:21px; height:21px; }
    .shero { display:flex; flex-direction:column; align-items:center; text-align:center;
      gap:11px; padding:26px 18px 16px; }
    .shero .sq { width:60px; height:60px; flex-basis:auto; border-radius:16px; font-size:15px; }
    .shero h1 { font-size:21px; font-weight:700; color:var(--tp); letter-spacing:-0.3px; }
    .shero-desc { font-size:13px; color:var(--tm); line-height:1.5; max-width:280px; }
    .shero-count { font-size:12.5px; color:var(--tm); margin-top:-5px; display:flex; align-items:center; gap:5px; }
    .sortbar { display:flex; justify-content:flex-end; padding:4px 18px 6px; }
    .sortbtn { background:var(--surface); border:1px solid var(--line); color:var(--tm);
      font-size:12px; font-weight:500; padding:7px 13px; border-radius:9px; cursor:pointer; }
    .searchwrap { padding:8px 18px 4px; }
    .searchbar { display:flex; align-items:center; gap:9px; background:var(--surface);
      border-radius:12px; padding:11px 14px; }
    .searchbar svg { width:18px; height:18px; color:var(--dim); flex:0 0 auto; }
    .searchbar input { flex:1; background:none; border:none; outline:none; color:var(--tp);
      font-size:15px; font-family:inherit; caret-color:var(--green); }
    .searchbar input::placeholder { color:var(--dim); }
    .searchbar input::-webkit-search-cancel-button { -webkit-appearance:none; appearance:none; }
    .clearbtn { flex:0 0 auto; background:none; border:none; color:var(--dim); cursor:pointer;
      font-size:20px; line-height:1; width:34px; height:34px; margin:-8px -8px -8px 0;
      align-items:center; justify-content:center; display:none; }
    .clearbtn.show { display:flex; }
    .sheet-cta { display:block; width:100%; margin-top:14px; background:var(--green);
      border:1px solid var(--green); color:#08120D; font-size:14px; font-weight:600;
      padding:12px; border-radius:11px; text-align:center; font-family:inherit; }
    .sheet-cta:active { opacity:0.8; }
"""

_GLASSES = ('<svg width="26" height="16" viewBox="0 0 26 16" fill="none">'
    '<circle cx="7.5" cy="8" r="4.5" stroke="#15B98A" stroke-width="1.5"/>'
    '<circle cx="18.5" cy="8" r="4.5" stroke="#15B98A" stroke-width="1.5"/>'
    '<line x1="12" y1="8" x2="14" y2="8" stroke="#15B98A" stroke-width="1.5" stroke-linecap="round"/>'
    '<line x1="3" y1="8" x2="1.5" y2="8" stroke="#15B98A" stroke-width="1.5" stroke-linecap="round"/>'
    '<line x1="23" y1="8" x2="24.5" y2="8" stroke="#15B98A" stroke-width="1.5" stroke-linecap="round"/></svg>')

_IC_HOME = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 11l9-8 9 8"/><path d="M5 10v10h14V10"/></svg>'
_IC_SEARCH = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.5" y2="16.5"/></svg>'
_IC_SAVED = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21l-7-5-7 5V5a2 2 0 012-2h10a2 2 0 012 2z"/></svg>'
_IC_INFO = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><line x1="12" y1="11" x2="12" y2="16"/><line x1="12" y1="8" x2="12" y2="8"/></svg>'


def _bottom_nav(active: str) -> str:
    def item(key, href, label, icon):
        on = " on" if key == active else ""
        return f'<a class="nav-i{on}" href="{href}">{icon}<span>{label}</span></a>'
    return ('<nav class="nav">'
            + item("home", "index.html", "Home", _IC_HOME)
            + item("search", "search.html", "Search", _IC_SEARCH)
            + item("saved", "saved.html", "Saved", _IC_SAVED)
            + '</nav>')


def _lib_page(title: str, body: str, active: str, extra_script: str = "") -> str:
    return (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n'
        '  <meta charset="UTF-8">\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">\n'
        '  <meta name="description" content="Podcast highlights, distilled. Listen less, know more.">\n'
        '  <meta property="og:title" content="Reading.Sis">\n'
        '  <meta property="og:description" content="Podcast highlights, distilled. Listen less, know more.">\n'
        f'  <title>{_t(title)}</title>\n'
        f'{FAVICON_LINKS}\n'
        f'{goatcounter_script()}\n'
        f'  <style>{LIB_CSS}</style>\n</head>\n<body>\n<div class="page">\n'
        '  <header class="hdr">'
        '<div class="hside left">' + _GLASSES + '</div>'
        '<div class="hcenter"><span class="wm">Reading<span>.Sis</span></span>'
        '<span class="tagline">' + _t(TAGLINE) + '</span></div>'
        '<div class="hside right"><button class="account" onclick="openAbout()" '
        'aria-label="About Reading.Sis">' + _IC_INFO + '</button></div>'
        '</header>\n'
        f'{body}\n</div>\n{_bottom_nav(active)}\n{_about_sheet()}\n{_ABOUT_SCRIPT}\n{extra_script}\n</body>\n</html>'
    )


def _about_sheet() -> str:
    return (
        '<div class="sheet-back" id="aboutBack" onclick="closeAbout(event)">'
        '<div class="sheet" role="dialog" aria-modal="true" aria-label="About Reading.Sis">'
        '<div class="sheet-grip"></div>'
        '<span class="wm">Reading<span>.Sis</span></span>'
        '<span class="tagline">' + _t(TAGLINE) + '</span>'
        '<p>The best moments and takeaways from long podcast episodes, read in a couple of '
        'minutes instead of listened to over a couple of hours. New episodes land here daily.</p>'
        '<p>Tap the bookmark on any episode to save it. Saves live on this device only — '
        'no account, nothing to sign up for.</p>'
        '<p>We\'re still early — your feedback helps shape what gets built next.</p>'
        '<a class="sheet-cta" href="feedback.html" onclick="closeAbout()">Share feedback</a>'
        '<button class="sheet-close" onclick="closeAbout()" style="margin-top:10px;">Close</button>'
        '</div></div>'
    )


_ABOUT_SCRIPT = (
    '<script>function openAbout(){document.getElementById("aboutBack").classList.add("show");}'
    'function closeAbout(e){if(e&&e.target!==e.currentTarget)return;'
    'document.getElementById("aboutBack").classList.remove("show");}</script>'
)


def _episode_row(e: dict) -> str:
    meta = e["show"]
    if e["guest"] and e["guest"].lower() != "various":
        meta += f" · {e['guest']}"
    meta += f" · {e['fdate']}"
    newtag = '<span class="newtag">NEW</span>' if e.get("new") else ""
    return (
        f'<a class="row" href="{e["url"]}">'
        f'<span class="chip" style="background:{e["color"]}">{_t(e["chip"])}</span>'
        f'<span class="row-main"><span class="row-title">{_t(e["title"])}</span>'
        f'<span class="row-meta">{newtag}{_t(meta)}</span></span>'
        f'<span class="chev">&rsaquo;</span></a>'
    )


# ── The four pages ────────────────────────────────────────────────────────────

TAGLINE = "Listen less, know more."


def build_library(tracker: dict) -> str:
    """Home: latest 5 episodes on top, then the shows grid."""
    eps, meta = _library_episodes(tracker)
    counts: dict[str, int] = {}
    for e in eps:
        counts[e["show"]] = counts.get(e["show"], 0) + 1

    latest = "".join(_episode_row(e) for e in eps[:5]) or \
        '<div class="empty">No digests published yet.</div>'

    genre_tabs = (
        '<div class="genre-tabs" id="genreTabs">'
        '<button class="gtab active" data-genre="all" onclick="filterGenre(this)">All</button>'
        '<button class="gtab" data-genre="tech" onclick="filterGenre(this)">Tech</button>'
        '<button class="gtab" data-genre="business" onclick="filterGenre(this)">Business</button>'
        '<button class="gtab" data-genre="storytelling" onclick="filterGenre(this)">Storytelling</button>'
        '<button class="gtab" data-genre="entertainment" onclick="filterGenre(this)">Entertainment</button>'
        '</div>'
    )

    cards = ""
    for p in PODCASTS:
        name = p["name"]
        m = meta[name]
        n = counts.get(name, 0)
        lang_badge = ('<span class="lang-he">HE</span>' if m["lang"] == "he"
                      else '<span class="lang-en">EN</span>')
        cards += (
            f'<a class="card" href="{m["slug"]}.html" data-genre="{m["genre"]}">'
            f'<span class="sq" style="background:{m["color"]}">{_t(m["chip"])}</span>'
            f'<span class="card-r"><span class="card-name">{_t(name)}</span>'
            f'<span class="card-meta">{lang_badge}'
            f'<span class="card-count">{n} episode{"" if n == 1 else "s"}</span></span></span></a>'
        )

    genre_filter_js = """<script>
function filterGenre(btn) {
  document.querySelectorAll('.gtab').forEach(function(b) { b.classList.remove('active'); });
  btn.classList.add('active');
  var genre = btn.dataset.genre;
  document.querySelectorAll('.grid .card').forEach(function(c) {
    c.style.display = (genre === 'all' || c.dataset.genre === genre) ? '' : 'none';
  });
}
</script>"""

    body = (
        f'  <div class="seclabel">Latest episodes</div>\n  <div class="rows">{latest}</div>\n'
        f'  <div class="seclabel">Shows</div>\n  {genre_tabs}\n  <div class="grid">{cards}</div>\n'
        f'{genre_filter_js}\n'
    )
    return _lib_page("Reading.Sis — Library", body, "home")


def build_podcast_pages(tracker: dict) -> dict:
    """One file per show: hero + all its episodes, newest→oldest with a flip toggle.
    Returns {filename: html}."""
    eps, meta = _library_episodes(tracker)
    by_slug: dict[str, list] = {}
    for e in eps:
        by_slug.setdefault(e["slug"], []).append(e)

    pages = {}
    for p in PODCASTS:
        name, m = p["name"], meta[p["name"]]
        show_eps = by_slug.get(m["slug"], [])
        rows = "".join(_episode_row(e) for e in show_eps) or \
            '<div class="empty">No episodes yet.</div>'
        n = len(show_eps)
        desc = p.get("description", "")
        rtl_attr = ' dir="rtl"' if m.get("lang") == "he" else ""
        desc_html = f'<div class="shero-desc"{rtl_attr}>{_t(desc)}</div>' if desc else ""
        _LAYERS = ('<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"'
                   ' stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
                   '<path d="M12 2L2 7l10 5 10-5-10-5z"/>'
                   '<path d="M2 12l10 5 10-5"/>'
                   '<path d="M2 17l10 5 10-5"/></svg>')
        body = (
            '  <div class="backrow"><a class="backlink" href="index.html">'
            '<span class="bk">&lsaquo;</span> All shows</a></div>\n'
            f'  <div class="shero"><span class="sq" style="background:{m["color"]}">{_t(m["chip"])}</span>'
            f'<h1>{_t(name)}</h1>{desc_html}'
            f'<div class="shero-count">{_LAYERS} {n} episode{"" if n == 1 else "s"}</div></div>\n'
            f'  <div class="sortbar"><button class="sortbtn" id="sortBtn" onclick="flip()">Newest first &#8645;</button></div>\n'
            f'  <div class="rows" id="rows">{rows}</div>\n'
        )
        script = ('<script>var asc=false;function flip(){var r=document.getElementById("rows");'
                  'var rows=Array.prototype.slice.call(r.querySelectorAll(".row"));'
                  'rows.reverse().forEach(function(x){r.appendChild(x);});asc=!asc;'
                  'document.getElementById("sortBtn").innerHTML=(asc?"Oldest first":"Newest first")+" &#8645;";}</script>')
        pages[f"{m['slug']}.html"] = _lib_page(f"Reading.Sis — {name}", body, "home", script)
    return pages


def build_search_page(tracker: dict) -> str:
    """Borderless search over an embedded offline index of every episode."""
    eps, _ = _library_episodes(tracker)
    index = [{"title": e["title"], "show": e["show"], "guest": e["guest"],
              "fdate": e["fdate"], "color": e["color"], "chip": e["chip"],
              "url": e["url"], "new": e["new"]} for e in eps]
    body = (
        '  <div class="searchwrap"><div class="searchbar">' + _IC_SEARCH +
        '<input id="q" type="search" placeholder="Search episodes, guests, shows" autofocus '
        'autocomplete="off" autocorrect="off">'
        '<button class="clearbtn" id="clearBtn" aria-label="Clear search">&times;</button>'
        '</div></div>\n'
        '  <div class="rows" id="results"></div>\n'
        '  <div class="empty" id="empty">Type to search the library.</div>\n'
    )
    script = (
        '<script>\n'
        'var EPISODES=' + json.dumps(index, ensure_ascii=False) + ';\n'
        'function esc(s){return (s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");}\n'
        'function rowHtml(e){var meta=e.show;if(e.guest&&e.guest.toLowerCase()!=="various")meta+=" \\u00b7 "+e.guest;'
        'meta+=" \\u00b7 "+e.fdate;var nt=e.new?\'<span class="newtag">NEW</span>\':"";'
        'return \'<a class="row" href="\'+e.url+\'"><span class="chip" style="background:\'+e.color+\'">\'+esc(e.chip)+'
        '\'</span><span class="row-main"><span class="row-title">\'+esc(e.title)+\'</span>\'+'
        '\'<span class="row-meta">\'+nt+esc(meta)+\'</span></span><span class="chev">\\u203a</span></a>\';}\n'
        'var q=document.getElementById("q"),res=document.getElementById("results"),emp=document.getElementById("empty"),clr=document.getElementById("clearBtn");\n'
        'function run(){var t=q.value.trim().toLowerCase();clr.classList.toggle("show",q.value.length>0);\n'
        'if(!t){res.innerHTML="";emp.style.display="block";emp.textContent="Type to search the library.";return;}\n'
        'var m=EPISODES.filter(function(e){return (e.title+" "+e.show+" "+e.guest).toLowerCase().indexOf(t)>-1;});\n'
        'res.innerHTML=m.map(rowHtml).join("");emp.style.display=m.length?"none":"block";emp.textContent="No matches.";}\n'
        'q.addEventListener("input",run);clr.addEventListener("click",function(){q.value="";run();q.focus();});q.focus();\n'
        '</script>'
    )
    return _lib_page("Reading.Sis — Search", body, "search", script)


def build_saved_page(tracker: dict) -> str:
    """Reads localStorage 'readingsis_saved' (set on episode pages) and renders
    the saved episodes, enriched from the embedded index by id."""
    eps, _ = _library_episodes(tracker)
    index = {e["id"]: {"title": e["title"], "show": e["show"], "guest": e["guest"],
                       "fdate": e["fdate"], "color": e["color"], "chip": e["chip"],
                       "url": e["url"], "new": False} for e in eps}
    body = (
        '  <div class="seclabel">Saved</div>\n  <div class="rows" id="saved"></div>\n'
        '  <div class="empty" id="empty">Nothing saved yet.<br>Tap the bookmark on any episode to keep it here.</div>\n'
    )
    script = (
        '<script>\n'
        'var INDEX=' + json.dumps(index, ensure_ascii=False) + ';\n'
        'function esc(s){return (s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");}\n'
        'function rowHtml(e){var meta=e.show;if(e.guest&&e.guest.toLowerCase()!=="various")meta+=" \\u00b7 "+e.guest;'
        'meta+=" \\u00b7 "+e.fdate;'
        'return \'<a class="row" href="\'+e.url+\'"><span class="chip" style="background:\'+e.color+\'">\'+esc(e.chip)+'
        '\'</span><span class="row-main"><span class="row-title">\'+esc(e.title)+\'</span>\'+'
        '\'<span class="row-meta">\'+esc(meta)+\'</span></span><span class="chev">\\u203a</span></a>\';}\n'
        'var saved=[];try{saved=JSON.parse(localStorage.getItem("readingsis_saved")||"[]");}catch(e){}\n'
        'var out=[];for(var i=saved.length-1;i>=0;i--){var s=saved[i];var e=INDEX[s.id];'
        'if(!e){e={title:s.title,show:s.show||"",guest:"",fdate:s.date||"",color:"#15B98A",chip:"?",url:s.url};}out.push(rowHtml(e));}\n'
        'document.getElementById("saved").innerHTML=out.join("");\n'
        'document.getElementById("empty").style.display=out.length?"none":"block";\n'
        '</script>'
    )
    return _lib_page("Reading.Sis — Saved", body, "saved", script)


def build_feedback_page() -> str:
    """Feedback page — auto-opens Tally popup on load; button is the fallback."""
    body = (
        '<div style="display:flex;flex-direction:column;align-items:center;'
        'text-align:center;padding:72px 24px 24px;gap:14px;">'
        '<p style="font-size:15px;font-weight:600;color:var(--tp);">Share your thoughts</p>'
        '<p style="font-size:13.5px;color:var(--tm);line-height:1.6;max-width:280px;">'
        'Help us build a better Reading.Sis — takes 2 minutes.</p>'
        '<button data-tally-open="68VOG5" data-tally-hide-title="1" '
        'data-tally-overlay="1" data-tally-auto-open="200" '
        'style="margin-top:8px;background:var(--green);color:#08120D;border:none;'
        'border-radius:11px;padding:13px 28px;font-size:15px;font-weight:700;'
        'cursor:pointer;font-family:inherit;">Open feedback form</button>'
        '</div>'
    )
    return _lib_page(
        "Reading.Sis — Feedback",
        body,
        "home",
        '<script src="https://tally.so/widgets/embed.js"></script>',
    )


def push_library(tracker: dict) -> None:
    """Build and publish the whole library site: home, search, saved, per-show pages."""
    files = {
        "index.html": build_library(tracker),
        "search.html": build_search_page(tracker),
        "saved.html": build_saved_page(tracker),
        "feedback.html": build_feedback_page(),
    }
    files.update(build_podcast_pages(tracker))
    for path, html in files.items():
        sha = None
        try:
            sha = gh_get(path).get("sha")
        except requests.HTTPError:
            pass
        gh_put(path, html.encode("utf-8"), "chore: update library", sha)
    print(f"  Library updated: {PAGES_BASE}/  ({len(files)} pages)")


# ══════════════════════════════════════════════════════════════════════════════
# WHATSAPP (GREEN API)
# ══════════════════════════════════════════════════════════════════════════════

def send_group_message(message: str) -> dict:
    url = (
        f"https://api.green-api.com"
        f"/waInstance{GREENAPI_ID}/sendMessage/{GREENAPI_TOKEN}"
    )
    r = requests.post(url, json={"chatId": GREENAPI_GROUP, "message": message}, timeout=15)
    r.raise_for_status()
    return r.json()


def send_personal_message(message: str) -> dict:
    """DM Noam directly — used by manual catch-up sends and the weekly
    digest. Never the group, regardless of what triggered the call."""
    chat = re.sub(r"\D", "", ALERT_TO_NOAM) + "@c.us"
    url = f"https://api.green-api.com/waInstance{GREENAPI_ID}/sendMessage/{GREENAPI_TOKEN}"
    r = requests.post(url, json={"chatId": chat, "message": message}, timeout=15)
    r.raise_for_status()
    return r.json()


# Scheduled send windows, IL time. Sun-Thu share one rhythm; Friday is
# front-loaded before Shabbat; Saturday is fully dark (no generate, send,
# digest, or fallback message — the queue simply rolls into Sunday).
# Python weekday(): Mon=0 … Sun=6.
SEND_SCHEDULE: dict[int, list[tuple[int, int]]] = {
    6: [(7, 30), (12, 30)],          # Sunday
    0: [(7, 30), (12, 30)],          # Monday
    1: [(7, 30), (12, 30)],          # Tuesday
    2: [(7, 30), (12, 30)],          # Wednesday
    3: [(7, 30), (12, 30)],          # Thursday
    4: [(7, 30), (11, 30), (14, 30)], # Friday — front-loaded before Shabbat
    5: [],                            # Saturday — dark
}
SATURDAY = 5


def _remaining_slots_today(now: datetime.datetime) -> list[datetime.datetime]:
    """Today's scheduled send times not yet passed — computed fresh from
    wall-clock every run (never a stored counter), so a missed/delayed run
    self-heals instead of desyncing the day. A grace window means the slot
    that just fired still counts itself despite dispatch/runner-startup lag."""
    grace = datetime.timedelta(minutes=10)
    out = []
    for h, m in SEND_SCHEDULE.get(now.weekday(), []):
        slot = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if slot >= now - grace:
            out.append(slot)
    return out


def _fallback_sis_message(p: dict) -> str:
    """Deterministic blurb if the Claude call fails — still friendly, no link
    (the footer carries the show/date/link separately), kept inside the
    10-20 word budget the real generator targets."""
    if p.get("lang") == "he":
        return "פרק חדש יצא —"
    guest = p.get("guest", "")
    g = f" with {guest}" if guest and guest.lower() != "various" else ""
    return f"New read{g} just landed — a good one for whenever you've got a minute."


def generate_sis_message(p: dict, idx: int, total: int, model: str = MODEL) -> str:
    """Write the warm, personal 'Sis' WhatsApp blurb for one episode.

    idx/total = this message's position in today's batch, so openers vary and
    don't re-greet awkwardly when 2–3 go out the same run. Returns ONLY the
    sentence (10-20 words) — the caller appends the footer (🎙️ show, date,
    link) separately. Falls back to a plain template on any error."""
    guest = p.get("guest", "")
    if guest and guest.lower() != "various":
        guest_part = f"Guest: {guest}\n"
    else:
        guest_part = "Format: panel show (regular hosts, no single guest)\n"

    if total > 1 and idx > 0:
        batch_note = (f"This is message {idx + 1} of {total} going out in the same run. "
                      "Do NOT open with a fresh greeting — frame it as another one right after "
                      "the last (e.g. 'and one more', 'also just out').")
    elif total > 1:
        batch_note = (f"You're sharing {total} reads in this run; this is the first. A light, "
                      "natural opener is fine — the next ones won't re-greet.")
    else:
        batch_note = "Just one read this run. A light, natural opener is fine."

    now = now_israel()
    time_context = f"It's currently {now.strftime('%-I:%M %p')} on a {now.strftime('%A')} in Israel."
    lang_note = (
        "\nLanguage: Write the message in Hebrew (עברית). "
        "The group members are Hebrew speakers."
    ) if p.get("lang") == "he" else ""

    prompt = f"""You are "Sis", the voice of Reading.Sis — texting a small group chat of friends \
about a podcast read you think they'd like. Not announcing content, not a newsletter — talking \
to friends.

Voice: warm and genuinely friendly, a little playful, never hype-y or corporate. Emoji only if \
it truly fits and never more than one — most messages should have none. Do NOT use 🎙️ — that's \
already in the footer this attaches to.

Time-of-day awareness: {time_context} Sometimes name the moment explicitly (e.g. "Sunday", \
"to close out the week"), and other times just let your word choice/energy carry it instead \
without naming it — vary which you do, don't lock into the same pattern every time.

Write ONE message, STRICTLY 10-20 words, about today's read — specific and natural, not generic. \
Do NOT include a link, the show name, or the episode title verbatim (those are added separately) \
— describe what it's about in your own words instead. Do NOT use markdown.

{batch_note}{lang_note}

{guest_part}Episode is about: {p.get('hook','') or p.get('title','')}

Return only the message text, nothing else."""
    try:
        client = Anthropic(api_key=ANTHROPIC_KEY)
        msg = client.messages.create(
            model=model, max_tokens=120,
            messages=[{"role": "user", "content": prompt}])
        text = (msg.content[0].text if msg.content else "").strip()
        word_count = len(text.split())
        title = (p.get("title") or "").strip()
        # QA the blurb: non-empty, no leaked placeholder/link (footer adds the
        # one and only link), within the strict word budget, and no verbatim
        # episode-title leak now that the footer no longer carries the title.
        bad = (not text or len(text) > 600 or "http" in text.lower()
               or any(tok in text for tok in PLACEHOLDER_TOKENS)
               or not (10 <= word_count <= 20)
               or (title and title.lower() in text.lower()))
        if not bad:
            return text
        print(f"  Sis message failed its check (words={word_count}) — using fallback")
    except Exception as e:
        print(f"  Sis message gen failed: {e} — using fallback")
    return _fallback_sis_message(p)


def _maybe_send_no_content_fallback(tracker: dict, now: datetime.datetime) -> bool:
    """If today ends with zero episodes sent across all its slots, send ONE
    message to the group at today's final scheduled send. Idempotent — won't
    double-send even if a run fires twice near the last slot."""
    today_str = str(now.date())
    if tracker.get("sent_count_today", {}).get("date") == today_str:
        sent_today = tracker["sent_count_today"]["count"]
    else:
        sent_today = 0
    if sent_today > 0:
        return False
    if len(_remaining_slots_today(now)) > 1:
        return False  # not the day's final slot yet
    if tracker.get("fallback_sent_date") == today_str:
        return False  # already sent today
    try:
        send_group_message("Hey, no new content today — hope you found something "
                            "else good to read. See you tomorrow.")
        tracker["fallback_sent_date"] = today_str
        print("  Sent no-content fallback message to the group.")
        return True
    except Exception as e:
        print(f"  No-content fallback send failed: {e}")
        return False


def send_pending(manual: bool = False) -> None:
    """Deliver pending WhatsApp messages for pages already live.

    Automatic mode (default, launchd-driven): wall-clock self-healing
    distribution — at most 3 sends per run, same-show deduped, 1-min
    staggered, oldest-published-first. Dark on Saturday. Fires the
    no-content fallback to the group if today ends with nothing sent.

    Manual mode (`manual=True`, via `--send --manual`): flushes the entire
    queue immediately for catch-up/testing, ignoring the cap and dedup.
    ALWAYS routes to Noam's personal WhatsApp only — never the group."""
    if not all([GREENAPI_ID, GREENAPI_TOKEN, GREENAPI_GROUP]):
        print("Green API not configured — cannot send.")
        return

    now = now_israel()
    if not manual and now.weekday() == SATURDAY:
        print("Saturday — fully dark, no send.")
        return

    tracker, tracker_sha = get_tracker()
    pending = tracker.get("pending_send", [])
    # Oldest-published-first, so a backlog drains in the order episodes
    # actually came out, not generation order.
    pending = sorted(pending, key=lambda p: p.get("published_at") or "")

    if not pending:
        print("Nothing pending to send.")
        if not manual:
            _maybe_send_no_content_fallback(tracker, now)
            save_tracker(tracker, tracker_sha)
        return

    # Pass 1: figure out which pages are actually sendable (live + pass QA).
    remaining = []
    sendable = []
    for p in pending:
        page_url = p["page_url"]
        print(f"── {p['id']} ──")
        deadline = time.time() + 300
        live = False
        while time.time() < deadline:
            try:
                if requests.head(page_url, timeout=10).status_code == 200:
                    live = True
                    break
            except requests.RequestException:
                pass
            time.sleep(10)
        if not live:
            print(f"  Page not live — keeping for next send run: {page_url}")
            remaining.append(p)
            continue
        # Final QA gate on the published page — never message a broken page.
        if not qa_live_page(page_url):
            print(f"  Live page failed QA — holding message: {page_url}")
            alert_noam(f"⚠️ Reading.Sis: {p['id']} is live but failed QA "
                       f"(bad JS or unfilled placeholder). Message held.")
            remaining.append(p)
            continue
        sendable.append(p)

    # Pass 2: pick this run's batch.
    if manual:
        batch = sendable
        deferred = []
    else:
        slots = _remaining_slots_today(now)
        target = min(3, len(sendable)) if sendable else 0
        batch, deferred = [], []
        seen_shows: set = set()
        for p in sendable:
            show = p.get("podcast")
            if len(batch) < target and show not in seen_shows:
                batch.append(p)
                seen_shows.add(show)
            else:
                deferred.append(p)  # same-show dedup or over target — next run
        print(f"  {len(slots)} slot(s) left today, {len(sendable)} sendable → "
              f"sending {len(batch)} this run, {len(deferred)} deferred")

    # Pass 3: generate each Sis message (aware of its position) and send.
    total = len(batch)
    sent = 0
    for idx, p in enumerate(batch):
        if not manual and idx > 0:
            time.sleep(60)  # 1-min stagger between sends in the same run
        body = generate_sis_message(p, idx, total)
        date_short = p.get("date_short") or p.get("date_str", "")
        footer = f"🎙️ {p.get('podcast','')}, {date_short}, {p['page_url']}"
        message = f"{body}\n\n{footer}"
        try:
            resp = send_personal_message(message) if manual else send_group_message(message)
            print(f"  WhatsApp sent ✓  {resp}")
            sent += 1
        except Exception as e:
            print(f"  WhatsApp failed: {e} — keeping for next send run")
            (deferred if not manual else remaining).append(p)

    tracker["pending_send"] = remaining + (deferred if not manual else [])

    if not manual:
        today_str = str(now.date())
        rec = tracker.get("sent_count_today") or {}
        if rec.get("date") != today_str:
            rec = {"date": today_str, "count": 0}
        rec["count"] += sent
        tracker["sent_count_today"] = rec
        if sent == 0:
            _maybe_send_no_content_fallback(tracker, now)

    save_tracker(tracker, tracker_sha)
    print(f"\nDone. {sent} sent, {len(tracker['pending_send'])} still pending.")


def weekly_digest() -> None:
    """Friday 16:00 IL — private DM to Noam only, never the group. Episode
    count per day and per show for the week just finished."""
    tracker, _ = get_tracker()
    now = now_israel()
    week_ago = now - datetime.timedelta(days=7)

    per_day: dict[datetime.date, int] = {}
    per_show: dict[str, int] = {}
    for ep in tracker.get("processed", []):
        if not isinstance(ep, dict) or ep.get("skipped"):
            continue
        pub = None
        ts = ep.get("published_at")
        if ts:
            try:
                pub = datetime.datetime.fromisoformat(ts)
            except ValueError:
                pub = None
        if pub is None and ep.get("date"):
            try:
                pub = datetime.datetime.strptime(ep["date"], "%Y-%m-%d")
            except ValueError:
                continue
        if pub is None or pub < week_ago or pub > now:
            continue
        per_day[pub.date()] = per_day.get(pub.date(), 0) + 1
        show = ep.get("podcast", "?")
        per_show[show] = per_show.get(show, 0) + 1

    total = sum(per_day.values())
    lines = [f"📊 Weekly digest — {total} episode(s) this week"]
    if per_day:
        lines.append("\nBy day:")
        for day in sorted(per_day):
            lines.append(f"  {day.strftime('%a %-d %b')}: {per_day[day]}")
    if per_show:
        lines.append("\nBy show:")
        for show, n in sorted(per_show.items(), key=lambda x: -x[1]):
            lines.append(f"  {show}: {n}")
    alert_noam("\n".join(lines))
    print("Weekly digest sent.")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def _queue_entry(episode: dict, pub_dt: datetime.datetime) -> dict:
    """A tracker `queued` record — carries everything a later run needs to
    re-process the episode without re-reading the RSS feed."""
    return {
        "id":           episode["id"],
        "podcast":      episode.get("podcast"),
        "title":        episode.get("title"),
        "date":         episode.get("date"),
        "description":  episode.get("description", ""),
        "pub_dt":       pub_dt.isoformat(),
        "duration_sec": episode.get("duration_sec"),
        "spotify_show": episode.get("spotify_show", ""),
        "lex_filter":   episode.get("lex_filter", False),
    }


def backfill(since: datetime.date, model: str = HAIKU) -> None:
    """One-time bulk fill: generate a page for every episode published since
    `since`, across all podcasts, to populate the public library.

    Differences from the daily pipeline:
      • ignores the send-day gate — processes everything since `since`
      • NEVER queues WhatsApp messages (no pending_send) — silent, no blast
      • uses cheap Haiku for generation (QA review still uses Sonnet)
      • resumable: skips pages already live (gh_exists) or already processed
    Run via a dedicated high-timeout workflow_dispatch, not the daily cron."""
    cutoff = datetime.datetime(since.year, since.month, since.day)
    print(f"Backfill since {since} using {model}\n")

    tracker, tracker_sha = get_tracker()
    processed_ids: set[str] = {
        (ep["id"] if isinstance(ep, dict) else ep) for ep in tracker.get("processed", [])
    }
    # Cross-run retry tracking for backfill, mirroring the daily pipeline's
    # "queued" bucket — a QA failure here persists its reason so the next
    # backfill dispatch doesn't blindly repeat the same mistake.
    bq_lookup: dict[str, dict] = {q["id"]: q for q in tracker.get("backfill_queued", [])}

    candidates: list[dict] = []
    for podcast in PODCASTS:
        print(f"Scanning {podcast['name']}…")
        # Per-show backfill_since overrides the global since date (used to cap
        # high-frequency shows like daily podcasts without a separate backfill run).
        show_since = podcast.get("backfill_since")
        show_cutoff = (
            datetime.datetime(*[int(x) for x in show_since.split("-")])
            if show_since else cutoff
        )
        eps = fetch_new_episodes(podcast, show_cutoff, processed_ids, set())
        print(f"  {len(eps)} to consider")
        candidates.extend(eps)

    print(f"\n{len(candidates)} episode(s) to backfill.\n")
    done = 0

    for episode in candidates:
        ep_id    = episode["id"]
        filename = f"{ep_id}.html"
        page_url = f"{PAGES_BASE}/{filename}"
        print(f"── {ep_id} ──")

        if gh_exists(filename):
            print("  Already live — marking processed")
            if ep_id not in processed_ids:
                tracker.setdefault("processed", []).append({"id": ep_id})
                processed_ids.add(ep_id)
            tracker["backfill_queued"] = [q for q in tracker.get("backfill_queued", []) if q["id"] != ep_id]
            bq_lookup.pop(ep_id, None)
            continue

        video_id = find_youtube_id(episode["title"], episode["podcast"], episode.get("show_format", ""))
        video_duration = None
        if video_id:
            if verify_youtube_match(video_id, episode):
                video_duration, _ = youtube_meta(video_id)
            else:
                video_id = None
        print(f"  YouTube: {video_id or 'none'}")

        dur = episode.get("duration_sec")
        if not video_id and dur and dur < SHORT_EPISODE_THRESHOLD_SEC:
            print(f"  Skipping — bonus/short episode ({dur}s, no video match)\n")
            tracker.setdefault("processed", []).append(
                {"id": ep_id, "skipped": True, "skip_reason": "short/bonus, no video"})
            processed_ids.add(ep_id)
            continue

        # Cross-run retry: an episode that failed QA on a previous backfill
        # dispatch carries its failure reason forward instead of blindly
        # repeating the same mistake.
        prior = bq_lookup.get(ep_id)
        prior_feedback = prior.get("last_qa_feedback") if prior else None

        transcript = get_transcript(video_id) if video_id else []
        content = generate_content(episode, transcript, video_id or "", model=model, qa_feedback=prior_feedback)
        if not content:
            print("  Generation failed — skipping\n")
            continue
        if content.get("skip"):
            print(f"  Lex filter skip: {content.get('skip_reason')}")
            tracker.setdefault("processed", []).append({"id": ep_id, "skipped": True})
            processed_ids.add(ep_id)
            continue

        passed, html, content, qa_issues = qa_episode(
            episode, content, video_id, video_duration, transcript, gen_model=model, prior_feedback=prior_feedback)
        for level, msg in qa_issues:
            print(f"  QA [{level}]: {msg}")
        if not passed:
            q = prior if prior is not None else {"id": ep_id, "podcast": episode.get("podcast"),
                                                  "title": episode.get("title")}
            if prior is None:
                tracker.setdefault("backfill_queued", []).append(q)
                bq_lookup[ep_id] = q
            q["qa_attempts"] = q.get("qa_attempts", 0) + 1
            blockers = "; ".join(m for l, m in qa_issues if l in ("blocker", "content"))
            q["last_qa_feedback"] = blockers
            print(f"  QA HELD (attempt {q['qa_attempts']}) — skipping (retry on next backfill run): {blockers}\n")
            if q["qa_attempts"] in (1, 3, 6):
                alert_noam(f"⚠️ Reading.Sis backfill QA held {ep_id} (attempt {q['qa_attempts']}). "
                           f"Not published until fixed.\nIssues: {blockers}")
            continue

        try:
            gh_put(filename, html.encode("utf-8"), f"feat: backfill {ep_id}")
            print(f"  Pushed: {page_url}")
        except Exception as e:
            print(f"  Push failed: {e} — skipping\n")
            continue

        tracker["backfill_queued"] = [q for q in tracker.get("backfill_queued", []) if q["id"] != ep_id]
        bq_lookup.pop(ep_id, None)

        # Backfill is SILENT — deliberately no pending_send entry.
        tracker.setdefault("processed", []).append({
            "id":           ep_id,
            "podcast":      episode.get("podcast"),
            "guest":        content.get("guest"),
            "title":        episode.get("title"),
            "date":         episode.get("date"),
            "published_at": episode["pub_dt"].isoformat() if episode.get("pub_dt") else None,
            "page_url":     page_url,
            "pushed_at":    str(datetime.date.today()),
        })
        processed_ids.add(ep_id)
        done += 1
        if done % 10 == 0:
            tracker_sha = save_tracker(tracker, tracker_sha)
            print(f"  …{done} pages done, tracker checkpointed")
        print()

    save_tracker(tracker, tracker_sha)
    print(f"\nBackfill complete: {done} new page(s). Rebuilding library…")
    push_library(tracker)
    print("Done.")


# Give up retrying for a video after this long — most YouTube uploads land
# same-day as the RSS publish, so 24h of retries across the day's generate
# runs should catch nearly all of them.
VIDEO_RETRY_WINDOW_HOURS = 24


def _retry_awaiting_video(tracker: dict) -> bool:
    """Re-attempt video search for episodes published without one. Doesn't
    delay anything — the page already shipped on RSS-description content.
    If a video has since appeared, regenerate with the real transcript and
    overwrite the page in place (same URL, no new WhatsApp message). Give up
    silently after VIDEO_RETRY_WINDOW_HOURS — the page just stays as-is."""
    bucket = tracker.get("awaiting_video", [])
    if not bucket:
        return False

    now = now_israel()
    still_waiting: list[dict] = []
    dirty = False

    for entry in bucket:
        ep_id = entry["id"]
        try:
            pub_dt = datetime.datetime.fromisoformat(entry["published_at"])
        except (KeyError, ValueError, TypeError):
            pub_dt = now  # malformed entry — treat as fresh rather than crash
        age = now - pub_dt
        if age > datetime.timedelta(hours=VIDEO_RETRY_WINDOW_HOURS):
            print(f"  [video-retry] {ep_id}: giving up after {VIDEO_RETRY_WINDOW_HOURS}h, no video found")
            dirty = True
            continue

        video_id = find_youtube_id(entry["title"], entry["podcast"], entry.get("show_format", ""))
        episode = dict(entry)
        episode["pub_dt"] = pub_dt
        episode.setdefault("date", pub_dt.strftime("%Y-%m-%d"))
        if video_id and verify_youtube_match(video_id, episode):
            video_duration, _ = youtube_meta(video_id)
            transcript = get_transcript(video_id)
            content = generate_content(episode, transcript, video_id)
            if content and not content.get("skip"):
                passed, html, content, qa_issues = qa_episode(
                    episode, content, video_id, video_duration, transcript)
                if passed:
                    filename = f"{ep_id}.html"
                    try:
                        current = gh_get(filename)
                        gh_put(filename, html.encode("utf-8"),
                               f"feat: add late-found video for {ep_id}", sha=current["sha"])
                        print(f"  [video-retry] {ep_id}: video found, page upgraded")
                        dirty = True
                        continue  # drop from bucket — done
                    except Exception as e:
                        print(f"  [video-retry] {ep_id}: found video but push failed: {e}")
                else:
                    print(f"  [video-retry] {ep_id}: video found but QA failed regeneration, retrying later")
            # fall through — keep waiting if generation/QA didn't pan out
        still_waiting.append(entry)

    tracker["awaiting_video"] = still_waiting
    return dirty


def _run_generate(window_override: datetime.timedelta | None = None,
                  preview_mode: bool = False) -> None:
    window = window_override or get_schedule()
    now   = now_israel()
    today = now.date()
    cutoff = now - window
    print(f"Date (Israel): {today}  |  window: {window}  (daily send)\n")

    tracker, tracker_sha = get_tracker()
    processed_ids: set[str] = {
        (ep["id"] if isinstance(ep, dict) else ep)
        for ep in tracker.get("processed", [])
    } | {
        ep["id"] for ep in tracker.get("preview", [])
        if isinstance(ep, dict) and ep.get("id")
    }
    queued_ids: set[str] = {ep["id"] for ep in tracker.get("queued", [])}

    # ── Discover new episodes from RSS ────────────────────────────────────────
    # found_by_podcast / outcomes feed the morning results DM (_send_run_summary).
    found_by_podcast: dict[str, int] = {}
    outcomes: list[dict] = []
    candidates: list[dict] = []
    # In preview_mode, take at most 1 episode per show (the most recent one) and
    # skip shows that already have a preview entry — this run is for first-look
    # previews, not bulk generation.
    shows_already_previewed = {
        ep.get("podcast") for ep in tracker.get("preview", []) if isinstance(ep, dict)
    } if preview_mode else set()
    for podcast in PODCASTS:
        print(f"Scanning {podcast['name']}…")
        if podcast.get("hold") and not preview_mode:
            print(f"  On hold — skipping daily scan")
            continue
        if preview_mode and podcast["name"] in shows_already_previewed:
            print(f"  Already in preview — skipping")
            continue
        eps = fetch_new_episodes(podcast, cutoff, processed_ids, queued_ids)
        if preview_mode and eps:
            eps = eps[:1]   # most recent only — one preview per show
        print(f"  {len(eps)} new episode(s)")
        if eps:
            found_by_podcast[podcast["name"]] = len(eps)
        candidates.extend(eps)

    # ── Re-evaluate queued episodes every run ─────────────────────────────────
    # "queued" now only holds QA-held episodes. RSS discovery skips anything
    # already queued, so they must be fed back in here to be retried. Work on a
    # COPY — never mutate the stored entry, or a datetime pub_dt leaks into the
    # tracker and breaks save_tracker if the retry also fails.
    existing_ids = {c["id"] for c in candidates}
    for q in tracker.get("queued", []):
        if q["id"] not in existing_ids and q["id"] not in processed_ids:
            cand = dict(q)
            raw_dt = cand.get("pub_dt")
            if isinstance(raw_dt, str):
                try:
                    cand["pub_dt"] = datetime.datetime.fromisoformat(raw_dt)
                except ValueError:
                    cand["pub_dt"] = datetime.datetime.strptime(cand["date"], "%Y-%m-%d")
            candidates.append(cand)

    if not candidates:
        print("\nNo new episodes today.")
        _send_run_summary(found_by_podcast, outcomes)
        return

    print(f"\n{len(candidates)} episode(s) to evaluate.\n")
    tracker_dirty = False
    preview_links: list[dict] = []  # populated only in preview_mode

    for episode in candidates:
        ep_id    = episode["id"]
        filename = f"{ep_id}.html"
        page_url = f"{PAGES_BASE}/{filename}"
        print(f"── {ep_id} ──")

        # Resolve pub_dt
        pub_dt = episode.get("pub_dt")
        if isinstance(pub_dt, str):
            try:
                pub_dt = datetime.datetime.fromisoformat(pub_dt)
            except ValueError:
                pub_dt = datetime.datetime.strptime(episode["date"], "%Y-%m-%d")
        elif pub_dt is None:
            pub_dt = datetime.datetime.strptime(episode.get("date", str(today)), "%Y-%m-%d")
        episode["pub_dt"] = pub_dt

        # Dedup: skip if page already exists on GitHub
        if gh_exists(filename):
            print(f"  Already published — marking processed")
            if ep_id not in processed_ids:
                tracker["processed"].append({"id": ep_id})
                tracker["queued"] = [q for q in tracker.get("queued", []) if q["id"] != ep_id]
                tracker_dirty = True
            continue

        # ── Find YouTube video (and verify it IS this episode) ───────────────
        video_id = find_youtube_id(episode["title"], episode["podcast"], episode.get("show_format", ""))
        video_duration = None
        if video_id:
            if verify_youtube_match(video_id, episode):
                video_duration, _ = youtube_meta(video_id)
            else:
                video_id = None
        print(f"  YouTube: {video_id or 'not found / not verified'}")

        # ── Short/bonus episode skip (no video + short RSS duration) ──────────
        dur = episode.get("duration_sec")
        if not video_id and dur and dur < SHORT_EPISODE_THRESHOLD_SEC:
            print(f"  Skipping — bonus/short episode ({dur}s, no video match)\n")
            tracker["processed"].append(
                {"id": ep_id, "skipped": True, "skip_reason": "short/bonus, no video"})
            outcomes.append({"id": ep_id, "podcast": episode.get("podcast"),
                             "title": episode.get("title"), "status": "skipped",
                             "detail": "short/bonus episode, no video match"})
            tracker_dirty = True
            continue

        # ── Get transcript ────────────────────────────────────────────────────
        transcript = get_transcript(video_id) if video_id else []
        print(f"  Transcript: {len(transcript)} segments")

        # ── Generate content via Claude ───────────────────────────────────────
        # A re-queued episode (failed QA on an earlier run) carries the reason
        # forward so this attempt doesn't blindly repeat the same mistake.
        prior_feedback = episode.get("last_qa_feedback")
        content = generate_content(episode, transcript, video_id or "", qa_feedback=prior_feedback)
        if not content:
            print("  Content generation failed — skipping\n")
            outcomes.append({"id": ep_id, "podcast": episode.get("podcast"),
                             "title": episode.get("title"), "status": "gen_failed",
                             "detail": "content generation returned nothing"})
            continue

        if content.get("skip"):
            print(f"  Skipped by Lex filter: {content.get('skip_reason')}\n")
            # Still mark as processed so we don't retry
            tracker["processed"].append({"id": ep_id, "skipped": True})
            outcomes.append({"id": ep_id, "podcast": episode.get("podcast"),
                             "title": episode.get("title"), "status": "skipped",
                             "detail": content.get("skip_reason")})
            tracker_dirty = True
            continue

        print(f"  Guest: {content.get('guest', '?')}")

        # ── QA stage: auto-fix and gate ───────────────────────────────────────
        passed, html, content, qa_issues = qa_episode(
            episode, content, video_id, video_duration, transcript, prior_feedback=prior_feedback)
        for level, msg in qa_issues:
            print(f"  QA [{level}]: {msg}")
        if not passed:
            # Hold the episode: don't publish broken, keep it queued for a retry
            # next run, and DM Noam. Its WhatsApp message waits until it's clean.
            q = next((x for x in tracker.setdefault("queued", []) if x["id"] == ep_id), None)
            if q is None:
                q = _queue_entry(episode, pub_dt)
                tracker["queued"].append(q)
                queued_ids.add(ep_id)
            q["qa_attempts"] = q.get("qa_attempts", 0) + 1
            blockers = "; ".join(m for l, m in qa_issues if l in ("blocker", "content"))
            q["last_qa_feedback"] = blockers
            print(f"  QA HELD (attempt {q['qa_attempts']}) — not publishing: {blockers}\n")
            if q["qa_attempts"] in (1, 3, 6):
                alert_noam(f"⚠️ Reading.Sis QA held {ep_id} (attempt {q['qa_attempts']}). "
                           f"Not sent until fixed.\nIssues: {blockers}")
            outcomes.append({"id": ep_id, "podcast": episode.get("podcast"),
                             "guest": content.get("guest"), "title": episode.get("title"),
                             "status": "held", "detail": blockers})
            tracker_dirty = True
            continue

        # ── Push HTML ─────────────────────────────────────────────────────────
        try:
            gh_put(filename, html.encode("utf-8"), f"feat: add {ep_id}")
            print(f"  Pushed: {page_url}")
        except Exception as e:
            print(f"  GitHub push failed: {e} — skipping\n")
            outcomes.append({"id": ep_id, "podcast": episode.get("podcast"),
                             "guest": content.get("guest"), "title": episode.get("title"),
                             "status": "push_failed", "detail": str(e)})
            continue

        # ── Queue WhatsApp for the 7 AM send phase ────────────────────────────
        # In preview_mode: DM Noam the links privately for review instead of
        # queuing for the group. Nothing reaches the group until he approves.
        if preview_mode:
            preview_links.append({
                "podcast":  episode.get("podcast"),
                "guest":    content.get("guest", ""),
                "title":    episode.get("title"),
                "page_url": page_url,
            })
            print("  Preview — DM to Noam only (not queued for group)")
        else:
            # Messages go out an hour later (run.py --send) so GitHub Pages has
            # comfortably finished deploying and every URL is verified live first.
            tracker.setdefault("pending_send", []).append({
                "id":           ep_id,
                "podcast":      episode.get("podcast"),
                "guest":        content.get("guest", ""),
                "title":        episode.get("title"),
                "date_str":     pub_dt.strftime("%-d %b %Y"),
                "date_short":   pub_dt.strftime("%b %-d"),   # footer format — no year
                "published_at": pub_dt.isoformat(),          # for oldest-first send ordering
                "hook":         (content.get("tldr") or "")[:300],
                "page_url":     page_url,
                "lang":         episode.get("lang", "en"),
            })
            print("  Queued for 7 AM send")

        # ── Update tracker ────────────────────────────────────────────────────
        if preview_mode:
            # Preview: page is live at its URL but NOT in the library and NOT
            # in processed. Tracked separately so the daily run doesn't
            # re-generate it. Noam reviews via private DM and approves manually.
            tracker.setdefault("preview", []).append({
                "id":        ep_id,
                "podcast":   episode.get("podcast"),
                "guest":     content.get("guest"),
                "title":     episode.get("title"),
                "date":      episode.get("date"),
                "page_url":  page_url,
                "pushed_at": str(today),
            })
            processed_ids.add(ep_id)  # prevent re-generation in this run
        else:
            tracker.setdefault("processed", []).append({
                "id":           ep_id,
                "podcast":      episode.get("podcast"),
                "guest":        content.get("guest"),
                "title":        episode.get("title"),
                "date":         episode.get("date"),
                "published_at": pub_dt.isoformat(),
                "page_url":     page_url,
                "pushed_at":    str(today),
            })
            tracker["queued"] = [q for q in tracker.get("queued", []) if q["id"] != ep_id]
            processed_ids.add(ep_id)
            if not video_id:
                # No video yet — a later run today (or tomorrow, within 24h)
                # will retry the search and upgrade this page in place.
                tracker.setdefault("awaiting_video", []).append({
                    "id":           ep_id,
                    "podcast":      episode.get("podcast"),
                    "slug_prefix":  episode.get("slug_prefix", ""),
                    "title":        episode.get("title"),
                    "date":         episode.get("date"),
                    "description":  episode.get("description", ""),
                    "duration_sec": episode.get("duration_sec"),
                    "spotify_show": episode.get("spotify_show", ""),
                    "lex_filter":   episode.get("lex_filter", False),
                    "show_format":  episode.get("show_format", "interview"),
                    "published_at": pub_dt.isoformat(),
                })
        outcomes.append({"id": ep_id, "podcast": episode.get("podcast"),
                         "guest": content.get("guest"), "title": episode.get("title"),
                         "status": "preview" if preview_mode else "published"})
        tracker_dirty = True
        print()

    if not preview_mode:
        print("\nRe-checking episodes still awaiting a video match…")
        if _retry_awaiting_video(tracker):
            tracker_dirty = True

    if tracker_dirty:
        save_tracker(tracker, tracker_sha)
        print("Tracker saved.")
        if not preview_mode:
            # Only rebuild the library for real publishes — preview episodes
            # must not appear in the library until Noam approves them.
            try:
                push_library(tracker)
            except Exception as e:
                print(f"  Library update failed: {e}")

    if preview_mode:
        # Build one link per show: prefer freshly generated, fall back to most
        # recent existing preview entry (e.g. crime-junkie already in bucket).
        preview_by_podcast: dict[str, dict] = {}
        for ep in sorted(tracker.get("preview", []),
                         key=lambda e: e.get("date", ""), reverse=True):
            pod = ep.get("podcast", "")
            if pod not in preview_by_podcast:
                preview_by_podcast[pod] = {
                    "podcast": pod, "guest": ep.get("guest", ""),
                    "title": ep.get("title", ""), "page_url": ep.get("page_url", ""),
                }
        for item in preview_links:          # freshly generated always wins
            preview_by_podcast[item["podcast"]] = item
        if preview_by_podcast:
            lines = ["🔍 Preview episodes ready — reply with show names to approve for the group:\n"]
            for item in preview_by_podcast.values():
                guest = f" w/ {item['guest']}" if item.get("guest") and item["guest"] not in ("Various", "") else ""
                lines.append(f"• {item['podcast']}{guest}\n  {item['page_url']}")
            alert_noam("\n".join(lines))

    if not preview_mode:
        _send_run_summary(found_by_podcast, outcomes)
    print("\nDone.")


def main() -> None:
    """6 AM generate phase. Bookends the run with two DMs to Noam: one when it
    starts, one with the results — and a failure DM if the run crashes, so a
    silent morning never masks a broken pipeline."""
    run = _run_label()
    alert_noam(f"hey — {run} run just kicked off, checking the feeds for new episodes now.")
    try:
        _run_generate()
    except Exception as e:
        alert_noam(f"❌ the {run} run hit an error and stopped early: {e}. "
                   f"nothing sent — needs a look.")
        raise


def patch_readmore_7d() -> None:
    """Patch episode pages published in the last 7 days to use the inline Read more truncation.

    Replaces the old vertical-fade CSS + JS with the new binary-search truncation + bottom sheet.
    Idempotent — skips pages that already contain the new pattern.
    """
    tracker, _ = get_tracker()
    cutoff = (datetime.datetime.utcnow() - datetime.timedelta(days=7)).date()

    targets = []
    for ep in tracker.get("processed", []):
        pub = ep.get("published_at")
        if not pub or ep.get("skipped"):
            continue
        try:
            pub_date = datetime.datetime.fromisoformat(str(pub)[:10]).date()
        except (ValueError, TypeError):
            continue
        if pub_date >= cutoff:
            targets.append(ep["id"])

    if not targets:
        print("  patch-readmore: no episodes found in last 7 days")
        return

    print(f"  patch-readmore: {len(targets)} episode(s) since {cutoff}")

    OLD_CSS = (
        '    .moment-quote { font-size: 13px; font-style: italic; color: var(--text-primary);'
        ' line-height: 1.5; margin-bottom: 8px; max-height: 9em; overflow: hidden; }\n'
        '    .moment-quote.fade { -webkit-mask-image: linear-gradient(180deg, #000 70%, transparent 100%);'
        ' mask-image: linear-gradient(180deg, #000 70%, transparent 100%); }'
    )
    NEW_CSS = (
        '    .moment-quote { font-size: 13px; font-style: italic; color: var(--text-primary);'
        ' line-height: 1.5; margin-bottom: 8px; }\n'
        '    .moment-rm-link { color: var(--text-primary); text-decoration: underline; cursor: pointer; }\n'
        '    .qsheet-back { position: fixed; inset: 0; z-index: 200; background: rgba(0,0,0,0.55);'
        ' display: none; align-items: flex-end; justify-content: center; }\n'
        '    .qsheet-back.show { display: flex; }\n'
        '    .qsheet { width: 100%; max-width: 430px; background: var(--card-bg);'
        ' border-top-left-radius: 20px; border-top-right-radius: 20px;'
        ' border-top: 1px solid var(--border); padding: 10px 22px calc(26px + env(safe-area-inset-bottom)); }\n'
        '    .qsheet-grip { width: 36px; height: 4px; border-radius: 2px; background: var(--border); margin: 0 auto 18px; }\n'
        '    .qsheet-speaker { font-size: 10px; font-weight: 700; color: var(--text-dim);'
        ' text-transform: uppercase; letter-spacing: 0.8px; margin-bottom: 12px; }\n'
        '    .qsheet-quote { font-size: 14px; font-style: italic; color: var(--text-primary);'
        ' line-height: 1.65; margin-bottom: 14px; }\n'
        '    .qsheet-ctx { font-size: 12.5px; color: var(--text-muted); line-height: 1.5; margin-bottom: 20px; }\n'
        '    .qsheet-close { width: 100%; background: var(--icon-bg); border: 1px solid var(--icon-border);'
        ' color: var(--text-primary); font-size: 14px; font-weight: 600; padding: 13px;'
        ' border-radius: 11px; cursor: pointer; font-family: inherit; }'
    )

    OLD_JS = (
        "  // Fade only quotes that actually overflow the 6-line cap (others render full).\n"
        "  Array.prototype.forEach.call(document.querySelectorAll('.moment-quote'), function(q) {\n"
        "    if (q.scrollHeight > q.clientHeight + 2) q.classList.add('fade');\n"
        "  });"
    )
    NEW_JS = (
        "  var _QL = Math.round(5 * 13 * 1.5);\n"
        "  Array.prototype.forEach.call(document.querySelectorAll('.moment-card'), function(card) {\n"
        "    var q = card.querySelector('.moment-quote');\n"
        "    if (!q || q.scrollHeight <= _QL + 2) return;\n"
        "    var sp = (card.querySelector('.moment-speaker') || {}).textContent || '';\n"
        "    var ctx = (card.querySelector('.moment-context') || {}).textContent || '';\n"
        "    var full = q.textContent;\n"
        "    var words = full.split(' '), lo = 0, hi = words.length - 1, mid;\n"
        "    while (lo < hi) {\n"
        "      mid = Math.ceil((lo + hi) / 2);\n"
        "      q.textContent = words.slice(0, mid).join(' ') + '...';\n"
        "      if (q.scrollHeight <= _QL + 2) lo = mid; else hi = mid - 1;\n"
        "    }\n"
        "    q.textContent = '';\n"
        "    q.appendChild(document.createTextNode(words.slice(0, lo).join(' ') + '... '));\n"
        "    var rm = document.createElement('span');\n"
        "    rm.className = 'moment-rm-link';\n"
        "    rm.textContent = 'Read more';\n"
        "    (function(s, f, c) { rm.onclick = function() { openQuote(s, f, c); }; })(sp, full, ctx);\n"
        "    q.appendChild(rm);\n"
        "  });\n"
        "  function openQuote(sp, q, ctx) {\n"
        "    document.getElementById('qsheetSp').textContent = sp;\n"
        "    document.getElementById('qsheetQ').textContent = q;\n"
        "    document.getElementById('qsheetCtx').textContent = ctx;\n"
        "    document.getElementById('qsheetBack').classList.add('show');\n"
        "  }\n"
        "  function closeQuote(e) {\n"
        "    if (e && e.target !== e.currentTarget) return;\n"
        "    document.getElementById('qsheetBack').classList.remove('show');\n"
        "  }"
    )

    QSHEET_HTML = (
        '<div class="qsheet-back" id="qsheetBack" onclick="closeQuote(event)">'
        '<div class="qsheet"><div class="qsheet-grip"></div>'
        '<div class="qsheet-speaker" id="qsheetSp"></div>'
        '<div class="qsheet-quote" id="qsheetQ"></div>'
        '<div class="qsheet-ctx" id="qsheetCtx"></div>'
        '<button class="qsheet-close" onclick="closeQuote()">Close</button>'
        '</div></div>\n'
    )

    patched = skipped = failed = 0
    for ep_id in targets:
        path = f"{ep_id}.html"
        try:
            data = gh_get(path)
            html = base64.b64decode(data["content"]).decode("utf-8")
            sha = data["sha"]
        except Exception as e:
            print(f"    skip {path}: fetch error — {e}")
            failed += 1
            continue

        if "moment-rm-link" in html:
            skipped += 1
            continue

        if OLD_CSS not in html:
            print(f"    skip {path}: CSS pattern not found (unexpected format)")
            skipped += 1
            continue

        html = html.replace(OLD_CSS, NEW_CSS)
        html = html.replace(OLD_JS, NEW_JS)
        html = html.replace("</body>", QSHEET_HTML + "</body>")

        try:
            gh_put(path, html.encode("utf-8"), "patch: add Read more to moment cards", sha)
            patched += 1
            print(f"    patched {path}")
        except Exception as e:
            print(f"    failed {path}: {e}")
            failed += 1

    print(f"  patch-readmore: done — {patched} patched, {skipped} skipped, {failed} failed")


if __name__ == "__main__":
    # `--send` delivers pending WhatsApp messages (automatic, scheduled phase).
    # `--send --manual` flushes the whole queue immediately for catch-up/testing
    #   — always to Noam's personal WhatsApp only, never the group.
    # `--weekly-digest` DMs Noam the week's episode counts (Friday 16:00 IL).
    # `--library` rebuilds and publishes index.html from the current tracker.
    # `--backfill SINCE=YYYY-MM-DD` bulk-generates pages since that date
    #   (silent — no WhatsApp). Default SINCE=2026-01-01.
    if "--send" in sys.argv:
        send_pending(manual="--manual" in sys.argv)
    elif "--weekly-digest" in sys.argv:
        weekly_digest()
    elif "--library" in sys.argv:
        tracker, _ = get_tracker()
        push_library(tracker)
    elif "--backfill" in sys.argv:
        since_str = "2026-01-01"
        for arg in sys.argv:
            if arg.startswith("SINCE="):
                since_str = arg.split("=", 1)[1]
        backfill(datetime.datetime.strptime(since_str, "%Y-%m-%d").date())
    elif "--patch-readmore" in sys.argv:
        patch_readmore_7d()
    elif "--preview-new" in sys.argv:
        # Generate the latest episode for shows with no processed episodes yet.
        # Pages are pushed to the library but nothing goes to the group —
        # links are DMed to Noam privately for review. He approves before
        # anything reaches the group.
        alert_noam("hey — preview-new run kicking off, generating first episodes for new shows. links coming your way for review.")
        try:
            _run_generate(window_override=datetime.timedelta(days=30), preview_mode=True)
        except Exception as e:
            alert_noam(f"❌ preview-new run hit an error: {e}")
            raise
    else:
        main()
