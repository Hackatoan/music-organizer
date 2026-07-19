# music-organizer

Watches one **main Tidal playlist**. When new tracks show up, Gemini classifies each
into **genre / subgenre / activities**, and the track is added to the matching
playlists (created on demand). Buckets are dynamic — the classifier is shown the
existing playlists each run so it reuses them instead of making near-duplicates.

Classification uses Google Gemini (cheap `gemini-2.5-flash`); playlist read/write is
direct Tidal via `tidalapi`. Requires a Tidal subscription and a Gemini API key.

## Setup

```bash
git clone https://github.com/Hackatoan/music-organizer.git
cd music-organizer
cp .env.example .env        # then put your GEMINI_API_KEY in it
docker compose build

# 1) one-time Tidal login (prints a link.tidal.com URL to approve)
docker compose run --rm music-organizer python organizer.py login

# 2) find your main playlist id and put it in config.yaml
docker compose run --rm music-organizer python organizer.py list-playlists
#   -> edit config.yaml: main_playlist_id: "<id>"

# 3) start the watcher
docker compose up -d
docker compose logs -f
```

## Clean up duplicates
Removes the *same song* appearing more than once by **normalized artist + title**
(e.g. an album cut and a remaster), keeping the first copy. It only removes the
playlist entry — the track/stream itself is untouched. Dry-run by default, and it
prints the REMOVE vs KEEP title of each pair so you can verify the match.

```bash
docker compose run --rm music-organizer python organizer.py dedupe            # preview main
docker compose run --rm music-organizer python organizer.py dedupe main --apply
docker compose run --rm music-organizer python organizer.py dedupe <id> --apply
# only remove specific stream ids (e.g. after reviewing a preview):
docker compose run --rm music-organizer python organizer.py dedupe main --apply --only 111,222
```
Live/remix/acoustic versions are deliberately kept (not treated as duplicates).

## How it works
- Source, classification and destinations are all Tidal via `tidalapi`.
- State lives in `data/state.json` (processed track ids + name→id playlist cache);
  the Tidal session is in `data/session.json`. Neither is committed.
- A track already added to a destination is not re-added (dedup per playlist).

## Config (`config.yaml`)
| key | meaning |
|-----|---------|
| `main_playlist_id` | the playlist you drop songs into |
| `poll_interval_seconds` | how often to check (default 300) |
| `process_existing_on_first_run` | organize the whole existing playlist on first run |
| `batch_size` | tracks per Gemini call |
| `max_activities` | max activity playlists per song |
| `naming.*` | playlist name templates, `{name}` = the picked label |
