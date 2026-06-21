# Reading.Sis — Claude Context

## ⚠️ READ THIS FIRST — single canonical copy

`scripts/run.py` in THIS folder is a **symlink**, not a real file. It points at
`/Users/noambenzbar/actions-runner/_work/reading-sis/reading-sis/scripts/run.py` — the actual
checkout GitHub Actions uses. Edit it from either path; it's the same file.

**Do not break the symlink.** On 2026-06-16, a separate session edited a stale, disconnected copy
of `run.py` in this folder (to add favicon support) and pushed it, silently reverting roughly a
day and a half of unrelated fixes from another session working in the actions-runner copy — SYSK
removal, QA model, duration-match tolerance, several content-quality fixes, all gone in one push.
A nearly identical thing happened earlier (2026-06-14→15) with `.github/workflows/reading-sis.yml`:
schedule triggers were deliberately removed in favor of launchd, then silently re-added by a later
session working from a stale copy that didn't have the removal.

**If you ever see `run.py` as a regular file here again (not a symlink), something went wrong —
stop and re-establish the symlink before editing anything.** Same logic applies to any other file
in this repo: always check `git log` / diff against the live GitHub state before assuming your
local copy is current, especially if it's been more than a few hours since you last touched it.

A second, more reliable option for genuinely parallel work: use separate git branches or
worktrees for concurrent sessions touching this repo, and merge deliberately — rather than two
sessions editing the same file from two different starting points and hoping the later push wins
cleanly.

See `SCHEDULE-PLAN.md` for Sprint 1's plan (shipped 2026-06-16) and the full incident writeup.
Working in 1-week sprints now — see "Sprint cadence" in `TASKS.md`. Active planning for the next
sprint goes in `SPRINT-2-PLAN.md`.

---

## What this is
Automated podcast-digest pipeline. Monitors 13 RSS feeds → transcribes via YouTube → generates a
styled HTML episode page → pushes to GitHub Pages → sends a personal WhatsApp message ("Sis" voice)
to the Reading.Sis group. Fully automated, multiple times a day Sun-Fri (see schedule below) —
Saturday is fully dark.

Live library: https://readingsis.github.io/reading-sis/
GitHub repo: github.com/readingsis/reading-sis (account login: readingsis)

---

## Key files in this project
```
scripts/run.py                  ← SYMLINK — see warning above. The entire pipeline (generation, QA, library, send)
scripts/requirements.txt        ← Python deps
.github/workflows/reading-sis.yml ← workflow_dispatch only — NO schedule trigger (launchd dispatches it, see below)
.github/workflows/backfill.yml  ← Manual backfill trigger (300min timeout)
.env                            ← Local secrets (NEVER push to repo)
task-prompt.md                  ← Agent instructions (NEVER push to repo)
readit-spec.md                  ← Old product spec (outdated architecture — ignore)
backfill-list.md                ← Episode manifest from the Jan 2026 backfill
SCHEDULE-PLAN.md                ← Sprint 1 plan (SHIPPED 2026-06-16) + incident log, kept for reference
SPRINT-2-PLAN.md                ← Active planning doc for the current sprint — add new ideas here
```

Runner logs: `/tmp/readingsis-generate.log`, `/tmp/readingsis-send.log`, `/tmp/readingsis-digest.log`
LaunchAgent plists: `~/Library/LaunchAgents/com.readingsis.{generate,send,digest}.plist`
Dispatch script: `~/actions-runner/dispatch-reading-sis.sh`
PAT (for launchd): `~/actions-runner/.readingsis-pat` (chmod 600 — NOT in ~/Documents)

---

## Architecture

```
Dispatch: launchd is the SOLE dispatcher — GitHub Actions has no schedule trigger, only
workflow_dispatch. launchd plists call dispatch-reading-sis.sh, which fires workflow_dispatch
via the GitHub API. Schedule (cut over 2026-06-16, Sprint 1):
  Sun-Thu: generate 7:00/12:00/19:00 IL, send 7:30/12:30/19:30 IL
  Friday:  generate 7:00/11:00/14:00 IL, send 7:30/11:30/14:30 IL, weekly digest 16:00 IL (DM only)
  Saturday: fully dark — no generate, send, digest, or fallback message
Plists: com.readingsis.{generate,send,digest}.plist. Phases: generate | send | send-manual
(flushes queue immediately, ALWAYS to Noam's personal WhatsApp, never the group) | weekly-digest
| library | preview-new.

Self-hosted runner "noam-mac" (~/actions-runner) — needed because YouTube blocks
GitHub-hosted IPs; the Mac's home IP works.

Storage:
  GitHub repo root → episode .html files + library pages + tracker.json
  tracker.json → pipeline state (processed_ids, queued, pending_send, etc.)

Transcripts: youtube_transcript_api (self-hosted runner, unblocked)
Generation: Anthropic Claude API (Sonnet for daily; Haiku for backfill)
QA: qa_episode() deterministic checks + qa_content_review() AI review
WhatsApp: Green API (instance 7107648283, bot number 972559746302)
Analytics: GoatCounter (site code "reading-sis")
```

---

## Podcasts (current)
| Name | Slug | Chip | Notes |
|------|------|------|-------|
| Lenny's Podcast | lennys | LP | |
| Pivot | pivot | PV | |
| All-In | all-in | AI | |
| Hard Fork | hard-fork | HF | |
| The Diary Of A CEO | doac | DOAC | Skip "most replayed" clip eps |
| Lex Fridman | lex-fridman | LX | Filter: tech/AI/science/business only |
| Crime Junkie | crime-junkie | CJ | true_crime format |
| Call Her Daddy | call-her-daddy | CHD | |
| SmartLess | smartless | SL | panel format |
| This Past Weekend w/ Theo Von | theo-von | TV | |
| Freakonomics Radio | freakonomics | FK | |
| Conan O'Brien Needs A Friend | conan | CB | |
| BigDeal | bigdeal | BD | |

All 13 shows are live in the daily cycle as of Sprint 1 (2026-06-16) — Noam reviewed the May 1st
backfill and approved lifting `hold` on the 7 shows added that round. SYSK and MrBallen were
evaluated and explicitly excluded (too high-volume, would dominate the feed). `hold: True` (if
ever set again on a future new show) blocks daily generate/send but not `--backfill`.

**Content filters (apply to all shows, all generate/backfill runs)**:
- Rerun filter (`RERUN_TITLE_RE`): skips titles matching FBF/flashback friday/re-release/rerun/
  replay/best of/throwback/encore at RSS discovery, before any generation cost.
- Short/bonus skip (`SHORT_EPISODE_THRESHOLD_SEC` = 25min): an episode under 25 min with no
  verified video match is skipped (marked processed with a skip note, never retried).

Show colors: hardcoded `_SHOW_RAMP` by PODCASTS index (gold→green).
Never auto-compute colors — the user chose fixed values.

---

## Models
- `MODEL = "claude-sonnet-4-6"` — daily generation, Sis messages
- `QA_MODEL = "claude-sonnet-4-6"` — QA content review (switched from Opus 4.8 for ~75% cost
  reduction; if this shows `claude-opus-4-8` again, that's a sign of the stale-copy bug above)
- `HAIKU = "claude-haiku-4-5"` — backfill generation only

---

## Non-obvious constraints

**macOS TCC blocks launchd from reading ~/Documents.**
The dispatch script reads the GitHub PAT from `~/actions-runner/.readingsis-pat` (chmod 600),
NOT from the .env in ~/Documents. If that file is missing, launchd fallback silently fails.

**GitHub's scheduled cron is unreliable** — it frequently skips or delays runs. This is *why*
there's no schedule trigger on the workflow at all anymore — launchd on noam-mac is the sole
dispatcher (see Architecture above), not a fallback alongside GitHub's cron.

**max_tokens must be ≥ 8000** in generate_content. With 3–10 scored takeaways + moments +
bio the JSON can be large; 2500 caused truncation and JSON parse failures.

**QA retry persists across runs** (built Sprint 1, 2026-06-16) — a held episode's failure reason
is stored on its `queued` (daily) or `backfill_queued` (backfill) tracker entry as
`last_qa_feedback`, and fed into the next regeneration attempt via `qa_feedback=` instead of
blindly repeating the same prompt. Escalation alert to Noam after 3 failed attempts.

**json.dumps in save_tracker uses `default=str`** to guard against datetime objects accidentally
leaking into the tracker (previously caused crashes).

**Episode IDs** use chronological-rank suffixes for same-day episodes: earliest = base slug,
second = -2, third = -3 (stable regardless of feed encounter order).

**Never push `.env` or `task-prompt.md` to the repo.**

---

## QA system
`qa_episode()` runs before every page publish:
1. Deterministic: placeholder leak check, `node --check` on inline JS, href="#" check,
   timestamp sanity, YouTube match (date ±5d + duration ±8%)
2. AI: `qa_content_review()` — cross-checks content against transcript (fabricated quotes,
   wrong guest, wrong fund names, etc.)
3. On fail: regenerate once → recheck. Still failing → HOLD (not published).
   `alert_noam()` DMs Noam on attempts 1/3/6.
4. `qa_live_page()` re-checks the published page before sending the WhatsApp message.

**QA also checks**: logo links to library (`class="logo" href="index.html"`),
takeaways count 3–10, Sis message self-check (word count 10-20, no URL/title leak, no placeholder).

**Video-retry bucket** (built Sprint 1) — an episode published without a verified video goes into
`tracker["awaiting_video"]`. Every subsequent generate run re-attempts the search; if a video shows
up within 24h, the page is regenerated with the real transcript and overwritten in place (same URL,
no new WhatsApp message). Gives up silently after 24h. See `_retry_awaiting_video()`.

---

## Sis voice (WhatsApp messages)
`generate_sis_message(p, idx, total)` — friendly, talking-to-friends tone, 10-20 words (strict),
time-of-day aware (mixes explicit day/time mentions with implicit energy shifts). No 🎙️ in the
body — that's reserved for the footer. Position-aware: idx/total so 2–3 same-run messages don't
repeat the greeting. Returns ONLY the sentence — the caller appends the footer:
`🎙️ {show}, {date e.g. "Jun 8"}, {link}` (episode title intentionally dropped from the footer).
Falls back to `_fallback_sis_message(p)` on any failure.

**Send distribution** (built Sprint 1) — `send_pending(manual=False)`: wall-clock self-healing
slot calculation via `_remaining_slots_today()` / `SEND_SCHEDULE`, max 3 sends/run, same-show
dedup (first per show this run, rest deferred), 1-min stagger between sends in the same run,
oldest-published-first. No-content fallback to the group if a whole day ends with zero sent
(`_maybe_send_no_content_fallback()`). `manual=True` (`--send --manual`) flushes the entire queue
immediately, ignoring cap/dedup, and ALWAYS routes to Noam's personal WhatsApp — never the group.
`weekly_digest()` (Friday 16:00 IL, DM only) reports episode counts per day/show for the week.

---

## Library structure (9 pages)
`index.html` — home: latest-5 feed + 2-col shows grid + sticky header + bottom nav
`search.html` — offline JSON search over all episodes
`saved.html` — localStorage-backed saved episodes
`<slug>.html` — one page per show (e.g. all-in.html, lennys.html)

Library auto-rebuilds on every pipeline run. Rebuild manually: `python3.11 scripts/run.py --library`

"New" badge (fixed Sprint 1) = true 24h since `published_at` (full timestamp, persisted on
newly-processed episodes going forward) — was date-granularity ~48h before. Episodes processed
before this existed just never show the badge (they're all long past "new" regardless).

---

## Backfill
`python3.11 scripts/run.py --backfill SINCE=YYYY-MM-DD`
Silent (never touches pending_send — no WhatsApp blast).
Uses Haiku for generation + Sonnet for QA review. Resumable. Checkpoints every 10 pages.
Needs a long-timeout workflow (backfill.yml, 300min) — daily workflow's 30min is too short.
