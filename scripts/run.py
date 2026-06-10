#!/usr/bin/env python3
"""
Reading.Sis — Automated podcast digest pipeline.
Runs via GitHub Actions Mon–Fri + Sun at 6 AM Israel time.
Finds new podcast episodes, generates HTML pages, pushes to GitHub Pages,
sends WhatsApp notification to the Reading.Sis group via Green API.
"""

from __future__ import annotations

import base64
import datetime
import json
import os
import re
import subprocess
import sys
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
        "name": "Design Better",
        "slug": "design-better",
        "rss": "https://feeds.megaphone.fm/designbetter",
        "spotify_show": "",
        "lex_filter": False,
    },
    {
        "name": "Lex Fridman Podcast",
        "slug": "lex-fridman",
        "rss": "https://lexfridman.com/feed/podcast/",
        "spotify_show": "",
        "lex_filter": True,  # Only tech/AI/science/business guests
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
    if is_sunday:
        return send_date <= today   # flush everything overdue
    return send_date == today


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


def save_tracker(tracker: dict, sha: str) -> None:
    content = json.dumps(tracker, indent=2, ensure_ascii=False).encode()
    gh_put("tracker.json", content, "chore: update tracker", sha or None)


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

    episodes = []
    for entry in feed.entries:
        if not getattr(entry, "published_parsed", None):
            continue

        pub_utc = datetime.datetime(*entry.published_parsed[:6])
        pub_il  = pub_utc + datetime.timedelta(hours=3)

        if pub_il < cutoff:
            break  # RSS is newest-first

        ep_id = f"{podcast['slug']}-{pub_il.strftime('%Y-%m-%d')}"
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
            "spotify_show": podcast["spotify_show"],
            "lex_filter":   podcast["lex_filter"],
        })

    return episodes


# ══════════════════════════════════════════════════════════════════════════════
# YOUTUBE
# ══════════════════════════════════════════════════════════════════════════════

def find_youtube_id(title: str, podcast_name: str) -> str | None:
    """Search YouTube via yt-dlp and return the video ID."""
    query = f"{podcast_name} {title}"
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
    """Fetch YouTube auto/manual transcript. Returns list of {t, text} dicts."""
    try:
        raw    = YouTubeTranscriptApi.get_transcript(video_id)
        result, words = [], 0
        for seg in raw:
            result.append({"t": int(seg["start"]), "text": seg["text"].strip()})
            words += len(seg["text"].split())
            if words >= max_words:
                break
        return result
    except Exception as e:
        print(f"  Transcript unavailable: {e}")
        return []


# ══════════════════════════════════════════════════════════════════════════════
# CONTENT GENERATION (CLAUDE API)
# ══════════════════════════════════════════════════════════════════════════════

def generate_content(episode: dict, transcript: list[dict], video_id: str) -> dict | None:
    """Call Claude to generate all page content. Returns structured dict or None."""
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
- For Lex Fridman episodes: set skip=true if the guest is an athlete, entertainer, pure philosopher, or politician with no AI/tech/science relevance. Give skip_reason.
- Return pure JSON. No markdown. No explanation."""

    try:
        msg = client.messages.create(
            model="claude-sonnet-4-6",
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
  <a class="icon-btn" href="YOUTUBE_URL" target="_blank" aria-label="Watch on YouTube">
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
  <a class="icon-btn" href="SPOTIFY_URL" target="_blank" aria-label="Listen on Spotify">
    <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor">
      <path d="M12 0C5.4 0 0 5.4 0 12s5.4 12 12 12 12-5.4 12-12S18.66 0 12 0zm5.521 17.34c-.24.359-.66.48-1.021.24-2.82-1.74-6.36-2.101-10.561-1.141-.418.122-.779-.179-.899-.539-.12-.421.18-.78.54-.9 4.56-1.021 8.52-.6 11.64 1.32.42.18.479.659.301 1.02zm1.44-3.3c-.301.42-.841.6-1.262.3-3.239-1.98-8.159-2.58-11.939-1.38-.479.12-1.02-.12-1.14-.6-.12-.48.12-1.021.6-1.141C9.6 9.9 15 10.561 18.72 12.84c.361.181.54.78.241 1.2zm.12-3.36C15.24 8.4 8.82 8.16 5.16 9.301c-.6.179-1.2-.181-1.38-.721-.18-.601.18-1.2.72-1.381 4.26-1.26 11.28-1.02 15.721 1.621.539.3.719 1.02.419 1.56-.299.421-1.02.599-1.559.3z"/>
    </svg>
  </a>
</div>

<script>
  var PAGE_URL   = 'PAGE_URL_JS';
  var PAGE_TITLE = 'EPISODE_TITLE_JS — Reading.Sis';

  function handleSave() {
    var btn = document.getElementById('bookmarkBtn');
    if (navigator.share) {
      navigator.share({title: PAGE_TITLE, url: PAGE_URL})
        .then(function() { btn.classList.add('saved'); }).catch(function() {});
    } else {
      navigator.clipboard && navigator.clipboard.writeText(PAGE_URL)
        .then(function() { btn.classList.add('saved'); });
    }
  }

  function handleShare() {
    var btn = document.getElementById('shareBtn');
    if (navigator.share) {
      navigator.share({title: PAGE_TITLE, url: PAGE_URL}).catch(function() {});
    } else {
      navigator.clipboard && navigator.clipboard.writeText(PAGE_URL).then(function() {
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


def _t(s: Any) -> str:
    """Escape HTML special chars for text content (not attributes)."""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


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
    return html


# ══════════════════════════════════════════════════════════════════════════════
# WHATSAPP (GREEN API)
# ══════════════════════════════════════════════════════════════════════════════

def send_whatsapp(episode: dict, content: dict, page_url: str) -> None:
    if not all([GREENAPI_ID, GREENAPI_TOKEN, GREENAPI_GROUP]):
        print("  Green API not configured — skipping WhatsApp")
        return

    # Compact format: podcast, guest, episode, date, link.
    guest  = content.get("guest", "")
    pub_dt = episode.get("pub_dt")
    date_str = (
        pub_dt.strftime("%-d %b %Y") if isinstance(pub_dt, datetime.datetime)
        else episode.get("date", "")
    )
    lines = [f"\U0001f399️ *{episode['podcast']}*"]
    if guest and guest.lower() != "various":
        lines.append(guest)
    lines += [f"_{episode['title']}_", date_str, page_url]
    message = "\n".join(lines)

    url = (
        f"https://api.green-api.com"
        f"/waInstance{GREENAPI_ID}/sendMessage/{GREENAPI_TOKEN}"
    )
    r = requests.post(url, json={"chatId": GREENAPI_GROUP, "message": message}, timeout=15)
    r.raise_for_status()
    print(f"  WhatsApp sent ✓  {r.json()}")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

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

    # ── On Sunday: also flush queued episodes ─────────────────────────────────
    if is_sunday:
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
                tracker.setdefault("queued", []).append({
                    "id":           ep_id,
                    "podcast":      episode.get("podcast"),
                    "title":        episode.get("title"),
                    "date":         episode.get("date"),
                    "description":  episode.get("description", ""),
                    "pub_dt":       pub_dt.isoformat(),
                    "spotify_show": episode.get("spotify_show", ""),
                    "lex_filter":   episode.get("lex_filter", False),
                })
                queued_ids.add(ep_id)
                tracker_dirty = True
            continue

        # ── Find YouTube video ────────────────────────────────────────────────
        video_id = find_youtube_id(episode["title"], episode["podcast"])
        print(f"  YouTube: {video_id or 'not found'}")

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

        # ── Build and push HTML ───────────────────────────────────────────────
        html = build_html(episode, content, video_id or "")
        try:
            gh_put(filename, html.encode("utf-8"), f"feat: add {ep_id}")
            print(f"  Pushed: {page_url}")
        except Exception as e:
            print(f"  GitHub push failed: {e} — skipping\n")
            continue

        # GitHub Pages takes 30-90s to deploy — don't send a link that 404s.
        deadline = time.time() + 300
        while time.time() < deadline:
            try:
                if requests.head(page_url, timeout=10).status_code == 200:
                    print("  Page is live")
                    break
            except requests.RequestException:
                pass
            time.sleep(10)
        else:
            print("  Page still not live after 5 min — sending anyway")

        # ── Send WhatsApp ─────────────────────────────────────────────────────
        try:
            send_whatsapp(episode, content, page_url)
        except Exception as e:
            print(f"  WhatsApp failed: {e}")

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

    print("\nDone.")


if __name__ == "__main__":
    main()
