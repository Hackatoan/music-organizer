# Music Organizer

Automatically sorts a Tidal playlist into genre / subgenre / activity playlists using an LLM.

☕ **Support:** [Buy Me a Coffee](https://buymeacoffee.com/hackatoa)

## Overview

Point it at a source Tidal playlist and it classifies each track with an LLM, then files tracks into the right genre, subgenre, and activity playlists — keeping a large library organized hands-free.

## Features

- LLM-based track classification
- Auto-sorts into genre / subgenre / activity playlists
- Mirrors Tidal hi-res lossless tracks into a dedicated "Hi-Res" playlist (no LLM needed)
- Runs on a schedule
- Offline `status` command: processed / queued / hi-res counts from saved state, no Tidal or Gemini calls (`--json` for scripts)

## Tech Stack

Python · tidalapi · Gemini · Docker

## Development

```bash
pip install -r requirements.txt
# configure Tidal + LLM credentials, then:
python3 organizer.py

# offline summary of what's organized and what's queued (no network, no login)
python3 organizer.py status          # or: status --json
# in the container:
docker compose run --rm music-organizer python organizer.py status
```

## Deployment

Docker on the homelab host; GHCR + Watchtower auto-deploy (default branch `master`).

## Support

If this project is useful to you, consider supporting development:

☕ **[Buy Me a Coffee](https://buymeacoffee.com/hackatoa)**

---

Part of the **[Hackatoa](https://hackatoa.com)** ecosystem — self-hosted apps, browser games, and bots. · [All repositories »](https://github.com/Hackatoan)
