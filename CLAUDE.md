# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-process bot that watches a list of Facebook groups (Florence/Prato area "free stuff" groups) for new posts matching keywords, and sends a Telegram alert when a match is found. It works by driving a real Chrome/Chromium instance with Selenium (there is no Facebook API involved) and scraping the top post of each group's feed.

## Running it

```bash
python main.py
```

- `start.bat` just runs the line above (used when launching on Windows).
- On first run, Chrome opens on the first configured group and the script pauses ~10s for a manual Facebook login; the session persists in the `chrome_profile/` directory (gitignored) so subsequent runs stay logged in.
- No `requirements.txt` exists; dependencies observed in code are `selenium`, `requests`, `psutil`.
- `main.py` hardcodes `options.binary_location = "/usr/bin/chromium"` and `Service("/usr/bin/chromedriver")`, i.e. it currently targets a Linux host (e.g. a Raspberry Pi — see commit "modifiche per rpi") even though development happens on Windows. Adjust these paths when running locally on Windows, or run it in the environment it's deployed to.
- There are no tests and no linter configured in this repo.

## Configuration files (all loaded/reloaded at runtime, not hardcoded)

- `groups.json` — list of `{name, url}` Facebook groups to monitor.
- `keywords.json` — words that trigger an alert when found in a post's text (case-insensitive substring match).
- `bad_keywords.json` — words that suppress an alert even if a keyword also matched (e.g. `"cerco"`, to skip "wanted" posts vs. "giving away" posts).
- `keywords.json` and `bad_keywords.json` are re-read from disk on every monitoring loop iteration (`reload_keywords()`), so they can be edited live without restarting the bot. `groups.json` is only read once at startup.
- `alerted_posts.json` — persisted set of post IDs already alerted on, used to avoid duplicate Telegram messages across restarts.
- `secrets.ini` has a `[telegram]` section with `bot_token`/`chat_id`, but it is currently **not read anywhere** — `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` are hardcoded near the top of `main.py` instead. Keep this in mind if credentials need to change: editing `secrets.ini` alone has no effect.

## Architecture: the scraping pipeline (`main.py`)

The core loop (`main()`) opens each group URL in turn, forces chronological sorting, and inspects only the single top post each cycle — it does not paginate or scroll. Per group, per cycle:

1. **Navigate**: `build_group_url()` appends `sorting_setting=CHRONOLOGICAL&locale=it_IT` to the group URL; `select_new_posts()` additionally tries to click Facebook's own sort-order UI ("Nuovi post") as a fallback, because the URL param isn't 100% reliable.
2. **Extract**: `get_top_post()` grabs the element `div[aria-posinset="1"]` and reads its `.text`.
3. **De-noise the raw text**: Facebook interleaves invisible Unicode formatting characters and scrambled single-character tokens into post text/timestamps specifically to defeat scrapers. This is handled by a chain of text-cleanup functions, in order:
   - `clean_facebook_text()` — strips Unicode categories `Mn`/`Me`/`Cf` (combining marks, enclosing marks, format chars).
   - `split_header_and_body()` / `looks_like_noise_segment()` — the post text looks like `<author><scrambled time>·<real content>`; this walks `·`-separated segments consuming everything that "looks like noise" (known UI labels like "Segui", or long runs of single-character tokens) until it finds the real body.
   - `extract_author()` — pulls the human author name out of the header using the same single-character-run heuristic.
   - `clean_description()` — trims the trailing UI footer (Mi piace/Commenta/Condividi), any embedded comment preview (which has its own name + relative-time pattern via `COMMENT_PREVIEW_RE` — relative times like "3 min" change between checks even for an unchanged post, which would otherwise defeat the dedup logic below), and trailing reaction/comment counters.
   
   If you need to adapt this to Facebook DOM/markup changes, the fragility is concentrated in these functions — the CSS selector (`div[aria-posinset="1"]`) and the sort-menu text hints in `select_new_posts()` are the other two spots likely to break first.
4. **Identify & dedupe**: `post_id` is built as `"<group_name> | <author> | <description>"` (truncated to 1000 chars) — there is no real Facebook post ID extracted for this purpose (though `extract_post_url()` does try to recover a real permalink via regex over the post's `<a>` hrefs, used only for the Telegram message link). Because the ID is text-derived, the cleanup step above matters for stable dedup across cycles.
5. **Filter & alert**: `process_top_post()` requires a `KEYWORDS` match, requires no `BAD_KEYWORDS` match, and requires the post ID not already be in `alerted_posts` (persisted to `alerted_posts.json` immediately on match, before sending) — then posts to Telegram via `send_telegram_message()`.

Within one cycle, `last_seen_ids[group_url]` also prevents reprocessing the same top post repeatedly across cycles even when it doesn't match keywords (it's checked before the keyword filter is even consulted).

Between full cycles over all groups, the loop sleeps `CHECK_INTERVAL` (30s) seconds, printing a `.` per second as a progress indicator.

`chiudi_chrome()` kills any process with "chrome" in its name at startup, to ensure a clean single Chrome instance under Selenium's control — be aware this affects any other running Chrome/Chromium processes on the machine.

## Unrelated script

`fontanelli.py` is a standalone, unrelated one-off script (queries the Overpass API for public drinking fountains in Florence) — it has no connection to the Facebook monitor and isn't invoked by `main.py`.
