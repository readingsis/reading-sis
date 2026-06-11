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

MODEL = "claude-sonnet-4-6"        # daily pipeline + QA review
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
PODCASTS = [
    {
        "name": "Lenny's Podcast",
        "slug": "lennys",
        "rss": "https://api.substack.com/feed/podcast/10845.rss",
        "spotify_show": "https://open.spotify.com/show/2dR1MUZEHCOnz1LVfNac0j",
        "lex_filter": False,
    },
    {
        "name": "Pivot",
        "slug": "pivot",
        "rss": "https://feeds.megaphone.fm/pivot",
        "spotify_show": "https://open.spotify.com/show/6UNmc4j2KaJTDr4gKXqYci",
        "lex_filter": False,
    },
    {
        "name": "All-In",
        "slug": "all-in",
        "rss": "https://rss.libsyn.com/shows/254861/destinations/1928300.xml",
        "spotify_show": "",
        "lex_filter": False,
    },
    {
        "name": "Hard Fork",
        "slug": "hard-fork",
        "rss": "https://feeds.simplecast.com/l2i9YnTd",
        "spotify_show": "https://open.spotify.com/show/44fllCS2FTFr2x1ouYggDj",
        "lex_filter": False,
    },
    {
        "name": "Lex Fridman Podcast",
        "slug": "lex-fridman",
        "rss": "https://lexfridman.com/feed/podcast/",
        "spotify_show": "",
        "lex_filter": True,  # Only tech/AI/science/business guests
    },
    {
        "name": "The Diary Of A CEO",
        "slug": "doac",
        "rss": "https://rss2.flightcast.com/xmsftuzjjykcmqwolaqn6mdn",
        "spotify_show": "",
        "lex_filter": False,
        # Skip the Friday "Most Replayed Moment" clip episodes — they're short
        # recaps of older episodes, not new full episodes.
        "skip_title_re": r"most replayed|moment[s]?:|highlight",
    },
]


# ══════════════════════════════════════════════════════════════════════════════
# SCHEDULING
# ══════════════════════════════════════════════════════════════════════════════

def now_israel() -> datetime.datetime:
    """Current datetime in Israel time (UTC+3, approximation)."""
    return datetime.datetime.utcnow() + datetime.timedelta(hours=3)


def get_schedule() -> tuple[datetime.timedelta | None, bool, bool]:
    """
    Returns (search_window, should_run, is_sunday).
    Saturday: should_run=False (silent day).
    Sunday: is_sunday=True (flush day — send all queued).
    Mon–Fri: standard 24h window.
    """
    day = now_israel().weekday()  # 0=Mon … 5=Sat, 6=Sun
    if day == 5:
        print("Saturday — silent day, exiting.")
        return None, False, False
    is_sunday = (day == 6)
    # Overlapping window on purpose: GitHub cron fires late and manual runs
    # shift the anchor, so a sharp 24h cutoff drops episodes. The tracker
    # dedupes anything already handled, so overlap is safe.
    hours = 72 if is_sunday else 36
    return datetime.timedelta(hours=hours), True, is_sunday


def get_send_date(pub_dt: datetime.datetime) -> datetime.date:
    """
    Send-day logic:
      Mon/Tue/Wed published → send next day
      Thu published before noon Israel → send Friday
      Thu published noon+ / Fri / Sat published → send Sunday
      Sun published → send Monday
    """
    day  = pub_dt.weekday()   # 0=Mon … 6=Sun
    hour = pub_dt.hour

    if day in (0, 1, 2):    # Mon, Tue, Wed
        return pub_dt.date() + datetime.timedelta(days=1)
    elif day == 3:           # Thursday
        delta = 1 if hour < 12 else 3   # +1=Fri  +3=Sun
        return pub_dt.date() + datetime.timedelta(days=delta)
    elif day == 4:           # Friday → Sunday
        return pub_dt.date() + datetime.timedelta(days=2)
    elif day == 5:           # Saturday → Sunday
        return pub_dt.date() + datetime.timedelta(days=1)
    else:                    # Sunday → Monday
        return pub_dt.date() + datetime.timedelta(days=1)


def should_send_today(send_date: datetime.date, today: datetime.date, is_sunday: bool) -> bool:
    # <= rather than ==: if a run was missed on the target day (Mac asleep,
    # cron skipped), the episode is overdue and should go out now, not wait
    # for the Sunday flush.
    return send_date <= today


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
    content = json.dumps(tracker, indent=2, ensure_ascii=False).encode()
    resp = gh_put("tracker.json", content, "chore: update tracker", sha or None)
    return resp.get("content", {}).get("sha", "")


# ══════════════════════════════════════════════════════════════════════════════
# EPISODE DISCOVERY (RSS)
# ══════════════════════════════════════════════════════════════════════════════

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
    seen_keys: set[str] = set()          # episode identities seen this fetch
    date_distinct: dict[str, int] = {}   # date -> count of DISTINCT episodes
    episodes = []
    for entry in feed.entries:
        if not getattr(entry, "published_parsed", None):
            continue

        pub_utc = datetime.datetime(*entry.published_parsed[:6])
        pub_il  = pub_utc + datetime.timedelta(hours=3)

        if pub_il < cutoff:
            break  # RSS is newest-first

        # Per-podcast title filter (e.g. DOAC "Most Replayed Moment" clip eps).
        if skip_re and re.search(skip_re, entry.title, re.IGNORECASE):
            print(f"  Skipping clip/recap episode: {entry.title[:60]}")
            continue

        # Episode identity = RSS guid (preferred) or title. Some feeds list the
        # same episode twice; that's a true duplicate, not a second episode.
        key = (getattr(entry, "id", None) or entry.title or "").strip()
        if key in seen_keys:
            continue  # duplicate feed entry — skip, do NOT mint a -2 page
        seen_keys.add(key)

        # Collision-safe ID: only DISTINCT episodes on the same day get a
        # suffix. First keeps slug-date (back-compat); 2nd+ get -2, -3…
        date_str = pub_il.strftime("%Y-%m-%d")
        idx = date_distinct.get(date_str, 0)
        date_distinct[date_str] = idx + 1
        base = f"{podcast['slug']}-{date_str}"
        ep_id = base if idx == 0 else f"{base}-{idx + 1}"
        if ep_id in processed_ids or ep_id in queued_ids:
            continue

        episodes.append({
            "id":           ep_id,
            "podcast":      podcast["name"],
            "slug_prefix":  podcast["slug"],
            "title":        entry.title,
            "description":  getattr(entry, "summary", ""),
            "pub_dt":       pub_il,
            "date":         pub_il.strftime("%Y-%m-%d"),
            "duration_sec": _parse_duration(getattr(entry, "itunes_duration", None)),
            "spotify_show": podcast["spotify_show"],
            "lex_filter":   podcast["lex_filter"],
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
        if abs(dur - rss_dur) > max(180, int(rss_dur * 0.08)):
            print(f"  Rejecting video {video_id}: duration {dur}s vs RSS {rss_dur}s")
            return False
    return True


def find_youtube_id(title: str, podcast_name: str) -> str | None:
    """Search YouTube for the episode video ID.

    Tries the official Data API first (works from datacenter IPs, needs
    YOUTUBE_API_KEY), then falls back to yt-dlp scraping (often blocked
    on GitHub Actions runners).
    """
    query = f"{podcast_name} {title}"

    api_key = os.environ.get("YOUTUBE_API_KEY", "")
    if api_key:
        try:
            r = requests.get(
                "https://www.googleapis.com/youtube/v3/search",
                params={"part": "id", "q": query, "type": "video",
                        "maxResults": 1, "key": api_key},
                timeout=20,
            )
            r.raise_for_status()
            items = r.json().get("items", [])
            if items:
                return items[0]["id"]["videoId"]
            return None
        except Exception as e:
            print(f"  YouTube Data API error: {e} — falling back to yt-dlp")

    try:
        result = subprocess.run(
            ["yt-dlp", f"ytsearch1:{query}", "--print", "%(id)s", "--no-download", "--quiet"],
            capture_output=True, text=True, timeout=45,
        )
        if result.returncode == 0:
            vid = result.stdout.strip().split("\n")[0].strip()
            if re.match(r"^[A-Za-z0-9_-]{11}$", vid):
                return vid
    except Exception as e:
        print(f"  YouTube search error: {e}")
    return None


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

def generate_content(episode: dict, transcript: list[dict], video_id: str,
                     model: str = MODEL) -> dict | None:
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
      "quote": "VERBATIM from transcript. If no transcript: '[No transcript — approximate] paraphrase'",
      "context": "1 sentence: when or why this was said",
      "timestamp_seconds": <integer>,
      "timestamp_display": "M:SS or H:MM:SS format"
    }}
  ],
  "takeaways": [
    {{
      "headline": "5-8 word bold headline",
      "body": "2-3 sentence explanation for a tech/product professional"
    }}
  ],
  "whatsapp_teaser": "One punchy sentence capturing the episode's most surprising or useful argument. Starts with guest first name (or show name for panels). This is what gets people to click.",
  "skip": false,
  "skip_reason": ""
}}

Hard rules:
- Provide exactly 5 moments and exactly 3 takeaways.
- Verbatim quotes only — never clean up or paraphrase. Keep "um", "like", filler words.
- Only use timestamps that actually appear in the transcript. If uncertain, use 0.
- For Lex Fridman episodes ONLY: keep the episode (skip=false) only if the guest's work is clearly in technology, AI/ML, computing, engineering, hard science (physics/biology/chemistry/math), business, startups, or economics. Set skip=true for everyone else — including historians, explorers, naturalists, musicians, artists, athletes, entertainers, religious figures, pure philosophers, and politicians — and give skip_reason. When in doubt for a Lex episode, skip.
- Return pure JSON. No markdown. No explanation."""

    try:
        msg = client.messages.create(
            model=model,
            max_tokens=2500,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        # Strip accidental markdown fences
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        return json.loads(raw)
    except Exception as e:
        print(f"  Claude error: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# HTML GENERATION
# ══════════════════════════════════════════════════════════════════════════════

# The canonical template. Placeholders use {{UPPER_SNAKE}} convention.
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
  <meta name="description" content="TLDR_FIRST_SENTENCE">
  <title>Reading.Sis — EPISODE_TITLE</title>
GOATCOUNTER_SCRIPT
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
    .logo { display: flex; align-items: center; gap: 8px; }
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
    .moment-card { flex: 0 0 240px; background: var(--card-bg); border: 1px solid var(--border); border-radius: 12px; padding: 14px; position: relative; min-height: 170px; }
    .moment-speaker { font-size: 10px; font-weight: 700; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.6px; margin-bottom: 8px; }
    .moment-quote { font-size: 13px; font-style: italic; color: var(--text-primary); line-height: 1.5; margin-bottom: 8px; }
    .moment-context { font-size: 11px; color: var(--text-dim); line-height: 1.4; margin-bottom: 28px; }
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
    <div class="logo">
      <svg width="26" height="16" viewBox="0 0 26 16" fill="none">
        <circle cx="7.5" cy="8" r="4.5" stroke="#0EB88A" stroke-width="1.5"/>
        <circle cx="18.5" cy="8" r="4.5" stroke="#0EB88A" stroke-width="1.5"/>
        <line x1="12" y1="8" x2="14" y2="8" stroke="#0EB88A" stroke-width="1.5" stroke-linecap="round"/>
        <line x1="3" y1="8" x2="1.5" y2="8" stroke="#0EB88A" stroke-width="1.5" stroke-linecap="round"/>
        <line x1="23" y1="8" x2="24.5" y2="8" stroke="#0EB88A" stroke-width="1.5" stroke-linecap="round"/>
      </svg>
      <span class="logo-text">Reading<span>.Sis</span></span>
    </div>
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

  // Fire a GoatCounter custom event (e.g. 'save', 'share', 'click-youtube').
  // Path is scoped to this episode so events break down per page. No-op when
  // GoatCounter isn't loaded (code unset, or blocked).
  function gcEvent(name) {
    if (window.goatcounter && window.goatcounter.count) {
      var ep = (location.pathname.split('/').pop() || 'page').replace(/\.html$/, '');
      window.goatcounter.count({path: ep + '-' + name, title: name, event: true});
    }
  }

  function handleSave() {
    var btn = document.getElementById('bookmarkBtn');
    gcEvent('save');
    if (navigator.share) {
      navigator.share({title: PAGE_TITLE, url: pageUrl})
        .then(function() { btn.classList.add('saved'); }).catch(function() {});
    } else {
      navigator.clipboard && navigator.clipboard.writeText(pageUrl)
        .then(function() { btn.classList.add('saved'); });
    }
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
</script>
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
    "MOMENTS_HTML", "TAKEAWAYS_HTML", "GOATCOUNTER_SCRIPT",
    "PAGE_URL_JS", "EPISODE_TITLE_JS", "YOUTUBE_URL", "SPOTIFY_URL",
]


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

Set overall_ok=false if the guest is wrong or any quote is fabricated. A generic TL;DR alone (tldr_ok=false) is a warning, not a failure."""
    client = Anthropic(api_key=ANTHROPIC_KEY)
    # Retry: the review occasionally returns an empty/non-JSON body. A None
    # here makes qa_episode fail-open (publishes unreviewed), so retry before
    # giving up rather than silently skipping the review.
    for attempt in range(3):
        try:
            msg = client.messages.create(
                model=MODEL, max_tokens=600,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = (msg.content[0].text if msg.content else "").strip()
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
            if raw:
                return json.loads(raw)
            print(f"  QA review empty response (attempt {attempt + 1})")
        except Exception as e:
            print(f"  QA content review error (attempt {attempt + 1}): {e}")
        time.sleep(2)
    return None


def qa_episode(episode: dict, content: dict, video_id: str | None,
               video_duration: int | None, transcript: list[dict],
               gen_model: str = MODEL) -> tuple[bool, str, dict, list]:
    """Run the full QA stage on one episode. Auto-fixes timestamps and, on a
    content-review failure, regenerates the content once. Returns
    (passed, html, content, issues). passed=False means real blockers remain
    and the page must not ship yet. The review itself always uses MODEL
    (Sonnet) for reliability; `gen_model` is used only for regeneration."""
    issues: list = []

    content = _fix_timestamps(content, video_duration, issues)

    # Content review (only meaningful when we have a transcript to check against).
    if transcript:
        review = qa_content_review(episode, content, transcript)
        if review and not review.get("overall_ok", True):
            issues.append(("content", f"review: {review.get('summary', 'content issue')}"))
            regen = generate_content(episode, transcript, video_id or "", model=gen_model)
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
    moments_html = ""
    for m in content.get("moments", []):
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

    # Build takeaways list
    takeaways_html = ""
    for i, tk in enumerate(content.get("takeaways", []), 1):
        takeaways_html += (
            f'    <div class="takeaway">\n'
            f'      <div class="takeaway-num">{i}</div>\n'
            f'      <div class="takeaway-text">'
            f'<strong>{_t(tk["headline"])} </strong>{_t(tk["body"])}</div>\n'
            f'    </div>\n'
        )

    tldr      = content.get("tldr", "")
    tldr_first = (tldr.split(".")[0] + ".") if tldr else ""
    read_time = content.get("read_time", 5)
    duration  = content.get("duration_str", "")

    html = HTML_TEMPLATE
    # JS/longer placeholders first: EPISODE_TITLE_JS and PAGE_URL_JS contain
    # EPISODE_TITLE / PAGE_URL as substrings and would be corrupted otherwise.
    js_title = episode["title"].replace("\\", "\\\\").replace("'", "\\'")
    html = html.replace("EPISODE_TITLE_JS",   js_title)
    html = html.replace("PAGE_URL_JS",        page_url)
    html = html.replace("TLDR_FIRST_SENTENCE", _t(tldr_first))
    # Text content
    html = html.replace("EPISODE_TITLE",       _t(episode["title"]))
    html = html.replace("PODCAST_NAME",        _t(episode["podcast"]))
    html = html.replace("GUEST_LINE",          _t(content.get("guest_line", "")))
    html = html.replace("PUBLISH_DATE_FORMATTED", pub_dt.strftime("%-d %b %Y"))
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
    return html


# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC LIBRARY (index.html)
# ══════════════════════════════════════════════════════════════════════════════

# Shareable landing page: every published episode, newest first. No analytics
# numbers are shown here — this link is meant for the WhatsApp group. Traffic is
# tracked silently via the same GoatCounter tag and viewed in GoatCounter's UI.
LIBRARY_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="description" content="Reading.Sis — the library of podcast digests.">
  <title>Reading.Sis — Library</title>
GOATCOUNTER_SCRIPT
  <style>
    :root {
      --bg: #111111; --card-bg: #181816; --green: #0EB88A;
      --text-primary: #F0EFE8; --text-secondary: #C8C7C0;
      --text-muted: #999990; --text-dim: #666660; --text-faint: #555550;
      --border: #2A2A28; --divider: #222220;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    html, body { background: var(--bg); color: var(--text-secondary); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; -webkit-font-smoothing: antialiased; }
    .page { max-width: 430px; margin: 0 auto; padding-bottom: 60px; min-height: 100vh; }
    .app-bar { position: sticky; top: 0; z-index: 100; background: var(--bg); border-bottom: 1px solid var(--divider); display: flex; align-items: center; padding: 14px 18px; }
    .logo { display: flex; align-items: center; gap: 8px; }
    .logo-text { font-size: 15px; font-weight: 700; color: var(--text-primary); letter-spacing: -0.3px; }
    .logo-text span { color: var(--green); }
    .hero { padding: 24px 18px 18px; border-bottom: 1px solid var(--divider); }
    .hero h1 { font-size: 24px; font-weight: 700; color: var(--text-primary); letter-spacing: -0.4px; margin-bottom: 6px; }
    .hero p { font-size: 13px; color: var(--text-muted); }
    .lib-list { padding: 8px 18px; }
    .lib-item { display: block; text-decoration: none; padding: 16px 0; border-bottom: 1px solid var(--divider); }
    .lib-item:active { opacity: 0.7; }
    .lib-badge { display: inline-block; background: rgba(14,184,138,0.12); color: var(--green); font-size: 9px; font-weight: 700; letter-spacing: 0.8px; text-transform: uppercase; padding: 3px 9px; border-radius: 20px; margin-bottom: 8px; }
    .lib-title { font-size: 15px; font-weight: 600; color: var(--text-primary); line-height: 1.35; margin-bottom: 4px; letter-spacing: -0.2px; }
    .lib-guest { font-size: 12px; color: var(--text-muted); margin-bottom: 4px; }
    .lib-date { font-size: 11px; color: var(--text-dim); }
    .empty { padding: 40px 18px; text-align: center; color: var(--text-dim); font-size: 13px; }
    .footer { padding: 24px 18px; text-align: center; font-size: 11px; color: var(--text-faint); }
  </style>
</head>
<body>
<div class="page">
  <div class="app-bar">
    <div class="logo">
      <svg width="26" height="16" viewBox="0 0 26 16" fill="none">
        <circle cx="7.5" cy="8" r="4.5" stroke="#0EB88A" stroke-width="1.5"/>
        <circle cx="18.5" cy="8" r="4.5" stroke="#0EB88A" stroke-width="1.5"/>
        <line x1="12" y1="8" x2="14" y2="8" stroke="#0EB88A" stroke-width="1.5" stroke-linecap="round"/>
        <line x1="3" y1="8" x2="1.5" y2="8" stroke="#0EB88A" stroke-width="1.5" stroke-linecap="round"/>
        <line x1="23" y1="8" x2="24.5" y2="8" stroke="#0EB88A" stroke-width="1.5" stroke-linecap="round"/>
      </svg>
      <span class="logo-text">Reading<span>.Sis</span></span>
    </div>
  </div>

  <div class="hero">
    <h1>Library</h1>
    <p>EPISODE_COUNT podcast digests, newest first.</p>
  </div>

  <div class="lib-list">
ITEMS_HTML
  </div>

  <div class="footer">Reading.Sis — podcast highlights, distilled.</div>
</div>
</body>
</html>"""


def build_library(tracker: dict) -> str:
    """Render the public index.html from processed episodes (newest first)."""
    eps = [
        ep for ep in tracker.get("processed", [])
        if isinstance(ep, dict) and ep.get("page_url") and ep.get("title")
        and not ep.get("skipped")
    ]
    # Newest first: prefer pushed_at, fall back to the episode date.
    eps.sort(key=lambda e: (e.get("pushed_at") or "", e.get("date") or ""), reverse=True)

    items = ""
    for ep in eps:
        guest = ep.get("guest") or ""
        guest_html = ""
        if guest and guest.lower() != "various":
            guest_html = f'      <div class="lib-guest">{_t(guest)}</div>\n'
        date_disp = ep.get("date") or ""
        try:
            date_disp = datetime.datetime.strptime(
                ep["date"], "%Y-%m-%d"
            ).strftime("%-d %b %Y")
        except (ValueError, KeyError, TypeError):
            pass
        items += (
            f'    <a class="lib-item" href="{ep["page_url"]}">\n'
            f'      <div class="lib-badge">{_t(ep.get("podcast", ""))}</div>\n'
            f'      <div class="lib-title">{_t(ep["title"])}</div>\n'
            f'{guest_html}'
            f'      <div class="lib-date">{date_disp}</div>\n'
            f'    </a>\n'
        )
    if not items:
        items = '    <div class="empty">No digests published yet.</div>\n'

    html = LIBRARY_TEMPLATE
    html = html.replace("ITEMS_HTML", items)
    html = html.replace("EPISODE_COUNT", str(len(eps)))
    html = html.replace("GOATCOUNTER_SCRIPT", goatcounter_script())
    return html


def push_library(tracker: dict) -> None:
    """Build and publish index.html, updating in place if it already exists."""
    html = build_library(tracker)
    sha = None
    try:
        sha = gh_get("index.html").get("sha")
    except requests.HTTPError:
        pass  # First publish — no existing file.
    gh_put("index.html", html.encode("utf-8"), "chore: update library", sha)
    print(f"  Library updated: {PAGES_BASE}/")


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


def send_pending() -> None:
    """7 AM phase (`--send`): deliver messages for pages the 6 AM generate
    phase produced, but only after verifying each URL is actually live.
    Anything not live (or failing to send) stays pending for the next run."""
    if not all([GREENAPI_ID, GREENAPI_TOKEN, GREENAPI_GROUP]):
        print("Green API not configured — cannot send.")
        return

    tracker, tracker_sha = get_tracker()
    pending = tracker.get("pending_send", [])
    if not pending:
        print("Nothing pending to send.")
        return

    remaining = []
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

        # Compact format: podcast, guest, episode, date, link.
        lines = [f"\U0001f399️ *{p['podcast']}*"]
        guest = p.get("guest", "")
        if guest and guest.lower() != "various":
            lines.append(guest)
        lines += [f"_{p['title']}_", p.get("date_str", ""), page_url]
        try:
            resp = send_group_message("\n".join(lines))
            print(f"  WhatsApp sent ✓  {resp}")
        except Exception as e:
            print(f"  WhatsApp failed: {e} — keeping for next send run")
            remaining.append(p)

    tracker["pending_send"] = remaining
    save_tracker(tracker, tracker_sha)
    print(f"\nDone. {len(pending) - len(remaining)} sent, {len(remaining)} still pending.")


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

    candidates: list[dict] = []
    for podcast in PODCASTS:
        print(f"Scanning {podcast['name']}…")
        # Pass empty queued set so we consider every not-yet-processed episode.
        eps = fetch_new_episodes(podcast, cutoff, processed_ids, set())
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
            continue

        video_id = find_youtube_id(episode["title"], episode["podcast"])
        video_duration = None
        if video_id:
            if verify_youtube_match(video_id, episode):
                video_duration, _ = youtube_meta(video_id)
            else:
                video_id = None
        print(f"  YouTube: {video_id or 'none'}")

        transcript = get_transcript(video_id) if video_id else []
        content = generate_content(episode, transcript, video_id or "", model=model)
        if not content:
            print("  Generation failed — skipping\n")
            continue
        if content.get("skip"):
            print(f"  Lex filter skip: {content.get('skip_reason')}")
            tracker.setdefault("processed", []).append({"id": ep_id, "skipped": True})
            processed_ids.add(ep_id)
            continue

        passed, html, content, qa_issues = qa_episode(
            episode, content, video_id, video_duration, transcript, gen_model=model)
        for level, msg in qa_issues:
            print(f"  QA [{level}]: {msg}")
        if not passed:
            print("  QA blocker — skipping (retry on next backfill run)\n")
            continue

        try:
            gh_put(filename, html.encode("utf-8"), f"feat: backfill {ep_id}")
            print(f"  Pushed: {page_url}")
        except Exception as e:
            print(f"  Push failed: {e} — skipping\n")
            continue

        # Backfill is SILENT — deliberately no pending_send entry.
        tracker.setdefault("processed", []).append({
            "id":        ep_id,
            "podcast":   episode.get("podcast"),
            "guest":     content.get("guest"),
            "title":     episode.get("title"),
            "date":      episode.get("date"),
            "page_url":  page_url,
            "pushed_at": str(datetime.date.today()),
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


def main() -> None:
    window, should_run, is_sunday = get_schedule()
    if not should_run:
        return

    now   = now_israel()
    today = now.date()
    cutoff = now - window
    print(f"Date (Israel): {today}  |  window: {window}  |  Sunday flush: {is_sunday}\n")

    tracker, tracker_sha = get_tracker()
    processed_ids: set[str] = {
        (ep["id"] if isinstance(ep, dict) else ep)
        for ep in tracker.get("processed", [])
    }
    queued_ids: set[str] = {ep["id"] for ep in tracker.get("queued", [])}

    # ── Discover new episodes from RSS ────────────────────────────────────────
    candidates: list[dict] = []
    for podcast in PODCASTS:
        print(f"Scanning {podcast['name']}…")
        eps = fetch_new_episodes(podcast, cutoff, processed_ids, queued_ids)
        print(f"  {len(eps)} new episode(s)")
        candidates.extend(eps)

    # ── Re-evaluate queued episodes every run ─────────────────────────────────
    # RSS discovery skips anything already queued, so queued episodes must be
    # fed back in here or they'd sit until Sunday. The send-day gate below
    # decides whether today is actually their day.
    existing_ids = {c["id"] for c in candidates}
    for q in tracker.get("queued", []):
        if q["id"] not in existing_ids and q["id"] not in processed_ids:
            # Rebuild pub_dt from stored ISO string
            raw_dt = q.get("pub_dt")
            if isinstance(raw_dt, str):
                try:
                    q["pub_dt"] = datetime.datetime.fromisoformat(raw_dt)
                except ValueError:
                    q["pub_dt"] = datetime.datetime.strptime(q["date"], "%Y-%m-%d")
            candidates.append(q)

    if not candidates:
        print("\nNo new episodes today.")
        return

    print(f"\n{len(candidates)} episode(s) to evaluate.\n")
    tracker_dirty = False

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

        # Send-day gate
        send_date = get_send_date(pub_dt)
        if not should_send_today(send_date, today, is_sunday):
            print(f"  Not send day yet (target: {send_date}) — queuing")
            if ep_id not in queued_ids:
                tracker.setdefault("queued", []).append(_queue_entry(episode, pub_dt))
                queued_ids.add(ep_id)
                tracker_dirty = True
            continue

        # ── Find YouTube video (and verify it IS this episode) ───────────────
        video_id = find_youtube_id(episode["title"], episode["podcast"])
        video_duration = None
        if video_id:
            if verify_youtube_match(video_id, episode):
                video_duration, _ = youtube_meta(video_id)
            else:
                video_id = None
        print(f"  YouTube: {video_id or 'not found / not verified'}")

        # ── Get transcript ────────────────────────────────────────────────────
        transcript = get_transcript(video_id) if video_id else []
        print(f"  Transcript: {len(transcript)} segments")

        # ── Generate content via Claude ───────────────────────────────────────
        content = generate_content(episode, transcript, video_id or "")
        if not content:
            print("  Content generation failed — skipping\n")
            continue

        if content.get("skip"):
            print(f"  Skipped by Lex filter: {content.get('skip_reason')}\n")
            # Still mark as processed so we don't retry
            tracker["processed"].append({"id": ep_id, "skipped": True})
            tracker_dirty = True
            continue

        print(f"  Guest: {content.get('guest', '?')}")

        # ── QA stage: auto-fix and gate ───────────────────────────────────────
        passed, html, content, qa_issues = qa_episode(
            episode, content, video_id, video_duration, transcript)
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
            print(f"  QA HELD (attempt {q['qa_attempts']}) — not publishing: {blockers}\n")
            if q["qa_attempts"] in (1, 3, 6):
                alert_noam(f"⚠️ Reading.Sis QA held {ep_id} (attempt {q['qa_attempts']}). "
                           f"Not sent until fixed.\nIssues: {blockers}")
            tracker_dirty = True
            continue

        # ── Push HTML ─────────────────────────────────────────────────────────
        try:
            gh_put(filename, html.encode("utf-8"), f"feat: add {ep_id}")
            print(f"  Pushed: {page_url}")
        except Exception as e:
            print(f"  GitHub push failed: {e} — skipping\n")
            continue

        # ── Queue WhatsApp for the 7 AM send phase ────────────────────────────
        # Messages go out an hour later (run.py --send) so GitHub Pages has
        # comfortably finished deploying and every URL is verified live first.
        tracker.setdefault("pending_send", []).append({
            "id":       ep_id,
            "podcast":  episode.get("podcast"),
            "guest":    content.get("guest", ""),
            "title":    episode.get("title"),
            "date_str": pub_dt.strftime("%-d %b %Y"),
            "page_url": page_url,
        })
        print("  Queued for 7 AM send")

        # ── Update tracker ────────────────────────────────────────────────────
        tracker.setdefault("processed", []).append({
            "id":        ep_id,
            "podcast":   episode.get("podcast"),
            "guest":     content.get("guest"),
            "title":     episode.get("title"),
            "date":      episode.get("date"),
            "page_url":  page_url,
            "pushed_at": str(today),
        })
        tracker["queued"] = [q for q in tracker.get("queued", []) if q["id"] != ep_id]
        processed_ids.add(ep_id)
        tracker_dirty = True
        print()

    if tracker_dirty:
        save_tracker(tracker, tracker_sha)
        print("Tracker saved.")
        # Refresh the public library whenever the episode list changed.
        try:
            push_library(tracker)
        except Exception as e:
            print(f"  Library update failed: {e}")

    print("\nDone.")


if __name__ == "__main__":
    # `--send` delivers pending WhatsApp messages (7 AM phase).
    # `--library` rebuilds and publishes index.html from the current tracker.
    # `--backfill SINCE=YYYY-MM-DD` bulk-generates pages since that date
    #   (silent — no WhatsApp). Default SINCE=2026-01-01.
    if "--send" in sys.argv:
        send_pending()
    elif "--library" in sys.argv:
        tracker, _ = get_tracker()
        push_library(tracker)
    elif "--backfill" in sys.argv:
        since_str = "2026-01-01"
        for arg in sys.argv:
            if arg.startswith("SINCE="):
                since_str = arg.split("=", 1)[1]
        backfill(datetime.datetime.strptime(since_str, "%Y-%m-%d").date())
    else:
        main()
