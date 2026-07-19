#!/usr/bin/env python3
"""
Tidal auto-organizer.

Watches one "main" Tidal playlist. Whenever new tracks appear, an LLM (Gemini)
classifies each into genre / subgenre / activities, and the track is added to the
matching genre, subgenre and activity playlists (created on demand).

Subcommands:
    python organizer.py login           # one-time Tidal device login
    python organizer.py list-playlists  # print your playlists + ids (pick the main one)
    python organizer.py run             # the watch loop (used by the container)
    python organizer.py once            # single pass, then exit (handy for testing)
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

import tidalapi
import yaml

DATA = Path(os.environ.get("DATA_DIR", "./data"))
SESSION_FILE = DATA / "session.json"
STATE_FILE = DATA / "state.json"
CONFIG_FILE = Path(os.environ.get("CONFIG_FILE", "./config.yaml"))
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


# ----------------------------- config / state -----------------------------

def load_config():
    cfg = yaml.safe_load(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}
    cfg.setdefault("main_playlist_id", "")
    cfg.setdefault("poll_interval_seconds", 300)
    cfg.setdefault("process_existing_on_first_run", True)
    cfg.setdefault("batch_size", 10)
    cfg.setdefault("gemini_model", "gemini-2.5-flash")
    cfg.setdefault("gemini_delay_seconds", 4)   # pause between classify calls (RPM safety)
    cfg.setdefault("tidal_delay_seconds", 0.5)  # pause between Tidal writes
    cfg.setdefault("add_chunk_size", 50)        # track ids per playlist.add() call
    cfg.setdefault("max_writes_per_pass", 0)    # 0 = unlimited; set e.g. 25 to trickle big backlogs
    cfg.setdefault("max_activities", 3)
    cfg.setdefault("fallback_playlist", "Unsorted")
    cfg.setdefault("naming", {})
    cfg["naming"].setdefault("genre", "Genre · {name}")
    cfg["naming"].setdefault("subgenre", "Sub · {name}")
    cfg["naming"].setdefault("activity", "Activity · {name}")
    return cfg


def load_state():
    if STATE_FILE.exists():
        s = json.loads(STATE_FILE.read_text())
    else:
        s = {}
    s.setdefault("processed", [])
    s.setdefault("playlists", {})    # name -> playlist_id
    s.setdefault("classified", {})   # track_id(str) -> [playlist names]  (cache)
    s.setdefault("initialized", False)
    s["_processed_set"] = set(s["processed"])
    return s


def save_state(s):
    out = {k: v for k, v in s.items() if not k.startswith("_")}
    out["processed"] = sorted(s["_processed_set"])
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, indent=2))
    tmp.replace(STATE_FILE)


# ----------------------------- tidal auth -----------------------------

def new_session():
    return tidalapi.Session()


def save_session(session):
    DATA.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(json.dumps({
        "token_type": session.token_type,
        "access_token": session.access_token,
        "refresh_token": session.refresh_token,
        "expiry_time": session.expiry_time.isoformat() if session.expiry_time else None,
    }))


def restore_session():
    session = new_session()
    if not SESSION_FILE.exists():
        return None
    d = json.loads(SESSION_FILE.read_text())
    from datetime import datetime
    expiry = datetime.fromisoformat(d["expiry_time"]) if d.get("expiry_time") else None
    ok = session.load_oauth_session(d["token_type"], d["access_token"],
                                    d["refresh_token"], expiry)
    if not ok or not session.check_login():
        return None
    # token may have been refreshed on load; persist latest
    save_session(session)
    return session


def cmd_login():
    session = new_session()
    login, future = session.login_oauth()
    url = getattr(login, "verification_uri_complete", None) or login.verification_uri
    if not url.startswith("http"):
        url = "https://" + url
    print("\n" + "=" * 60)
    print("  Open this URL and approve access to your Tidal account:")
    print("   ", url)
    print("  (code: %s)" % getattr(login, "user_code", "?"))
    print("=" * 60 + "\n", flush=True)
    future.result()  # blocks until approved or times out
    if not session.check_login():
        print("Login did not complete.")
        sys.exit(1)
    save_session(session)
    print("Logged in as user id:", session.user.id)
    print("Saved session ->", SESSION_FILE)


# ----------------------------- tidal helpers -----------------------------

def user_playlists(session):
    try:
        return list(session.user.playlists())
    except Exception:
        return list(session.user.playlist_and_favorite_playlists())


def cmd_list_playlists():
    session = restore_session()
    if not session:
        print("Not logged in. Run: python organizer.py login")
        sys.exit(1)
    print("Your playlists:")
    for pl in user_playlists(session):
        n = getattr(pl, "num_tracks", "?")
        print(f"  {pl.id}   {n:>5} tracks   {pl.name}")


def all_playlist_tracks(session, pid):
    pl = session.playlist(pid)
    out, offset = [], 0
    while True:
        chunk = pl.tracks(limit=100, offset=offset)
        if not chunk:
            break
        out.extend(chunk)
        offset += len(chunk)
        if len(chunk) < 100:
            break
    return out


def playlist_track_ids(session, pid):
    try:
        return {str(t.id) for t in all_playlist_tracks(session, pid)}
    except Exception:
        return set()


def ensure_playlist(session, state, name, pl_index, description=""):
    """Return playlist id for `name`, creating it if needed. pl_index maps
    known name->id from a single fresh fetch this run."""
    if name in state["playlists"]:
        return state["playlists"][name]
    if name in pl_index:
        state["playlists"][name] = pl_index[name]
        return pl_index[name]
    np = session.user.create_playlist(name, description or "Auto-organized by music-organizer")
    pid = np.id
    state["playlists"][name] = pid
    pl_index[name] = pid
    log("created playlist:", name, pid)
    time.sleep(0.5)   # be gentle when creating many playlists at once
    return pid


def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def bulk_add(session, pid, track_ids, cfg):
    """Add track_ids to playlist pid, skipping ones already present, in chunks
    with throttling and 429-aware retry. Returns count added."""
    existing = playlist_track_ids(session, pid)
    to_add = [tid for tid in track_ids if str(tid) not in existing]
    added = 0
    for chunk in chunks(to_add, cfg["add_chunk_size"]):
        for attempt in range(5):
            try:
                session.playlist(pid).add(chunk)
                added += len(chunk)
                break
            except Exception as e:
                code = getattr(getattr(e, "response", None), "status_code", None)
                wait = 30 if code == 429 else 2 ** attempt
                log(f"  tidal add retry ({code or e}) in {wait}s")
                time.sleep(wait)
        time.sleep(cfg["tidal_delay_seconds"])
    return added


# ----------------------------- gemini classify -----------------------------

CLASSIFY_INSTRUCTIONS = (
    "You are a music librarian. For each track, assign a primary genre, a more "
    "specific subgenre, and 0-{maxact} 'activity' moods it fits (e.g. Workout, "
    "Focus, Chill, Party, Drive, Sleep, Study, Gaming, Running). Use canonical, "
    "Title Case names. Prefer reusing a name from EXISTING_PLAYLISTS when it "
    "genuinely fits, to avoid near-duplicate buckets. If you genuinely cannot "
    "identify the track (obscure, a personal/user upload, no reliable info), set "
    'genre and subgenre to "Unknown" and activities to []. Otherwise give your '
    "best guess rather than Unknown."
)

# genre values that mean "could not classify" -> route to the fallback playlist
UNKNOWN_GENRES = {"", "unknown", "unclassified", "n/a", "na", "none", "other"}


def gemini_classify(cfg, tracks, existing_names):
    """tracks: list of dicts {index, artist, title, album}. Returns list aligned
    with 'index' of {genre, subgenre, activities}."""
    model = cfg["gemini_model"]
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    instr = CLASSIFY_INSTRUCTIONS.format(maxact=cfg["max_activities"])
    lines = [f'{t["index"]}. "{t["title"]}" by {t["artist"]} (album: {t["album"]})'
             for t in tracks]
    existing = "\n".join(sorted(existing_names)) or "(none yet)"
    prompt = (
        f"{instr}\n\nEXISTING_PLAYLISTS:\n{existing}\n\n"
        f"TRACKS:\n" + "\n".join(lines) +
        "\n\nReturn one object per track, echoing its 'index'."
    )
    schema = {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "index": {"type": "integer"},
                "genre": {"type": "string"},
                "subgenre": {"type": "string"},
                "activities": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["index", "genre", "subgenre", "activities"],
        },
    }
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": schema,
            "temperature": 0.2,
        },
    }
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_KEY},
        method="POST",
    )
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.loads(r.read())
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            arr = json.loads(text)
            return {int(o["index"]): o for o in arr}
        except urllib.error.HTTPError as e:
            wait = 45 if e.code == 429 else 2 ** attempt   # respect rate limit
            log(f"gemini HTTP {e.code}; retry in {wait}s")
            time.sleep(wait)
        except (urllib.error.URLError, KeyError, json.JSONDecodeError) as e:
            wait = 2 ** attempt
            log(f"gemini error ({e}); retry in {wait}s")
            time.sleep(wait)
    raise RuntimeError("gemini classification failed after retries")


# ----------------------------- main pass -----------------------------

def track_meta(t):
    try:
        artist = t.artist.name if t.artist else ", ".join(a.name for a in (t.artists or []))
    except Exception:
        artist = "Unknown"
    album = getattr(getattr(t, "album", None), "name", "") or ""
    return {"artist": artist or "Unknown", "title": t.name, "album": album}


def targets_for(cfg, cls):
    fb = cfg.get("fallback_playlist") or "Unsorted"
    if not cls:
        return [fb]
    names = []
    g = (cls.get("genre") or "").strip()
    s = (cls.get("subgenre") or "").strip()
    if g.lower() in UNKNOWN_GENRES:
        return [fb]
    if g:
        names.append(cfg["naming"]["genre"].format(name=g))
    if s and s.lower() not in UNKNOWN_GENRES and s.lower() != g.lower():
        names.append(cfg["naming"]["subgenre"].format(name=s))
    seen = set()
    for a in (cls.get("activities") or [])[: cfg["max_activities"]]:
        a = (a or "").strip()
        if a and a.lower() not in seen:
            seen.add(a.lower())
            names.append(cfg["naming"]["activity"].format(name=a))
    return names


def one_pass(cfg, session, state):
    pid = str(cfg["main_playlist_id"]).strip()
    if not pid:
        log("main_playlist_id not set; run list-playlists and set it in config.yaml")
        return
    tracks = all_playlist_tracks(session, pid)
    log(f"main playlist has {len(tracks)} tracks")

    # First run: optionally skip existing so we only organize songs added later.
    if not state["initialized"]:
        state["initialized"] = True
        if not cfg["process_existing_on_first_run"]:
            for t in tracks:
                state["_processed_set"].add(str(t.id))
            save_state(state)
            log("baseline set; existing tracks marked processed (skipping them).")
            return

    # ---- Phase 1: classify new tracks (cached + throttled) ----
    to_classify = [t for t in tracks
                   if str(t.id) not in state["_processed_set"]
                   and str(t.id) not in state["classified"]]
    if to_classify:
        log(f"classifying {len(to_classify)} new track(s)")
        existing_names = {pl.name for pl in user_playlists(session)} | set(state["playlists"])
        bs = cfg["batch_size"]
        for i in range(0, len(to_classify), bs):
            batch = to_classify[i:i + bs]
            payload = [dict(index=j, **track_meta(t)) for j, t in enumerate(batch)]
            try:
                results = gemini_classify(cfg, payload, existing_names)
            except Exception as e:
                log("classify batch failed; resuming next pass:", e)
                break
            for j, t in enumerate(batch):
                names = targets_for(cfg, results.get(j))
                state["classified"][str(t.id)] = names
                existing_names.update(names)
            save_state(state)
            log(f"  classified {min(i + bs, len(to_classify))}/{len(to_classify)}")
            if i + bs < len(to_classify):
                time.sleep(cfg["gemini_delay_seconds"])

    # ---- Phase 2: write to playlists (grouped, bulk, throttled) ----
    pending = [tid for tid in state["classified"] if tid not in state["_processed_set"]]
    if not pending:
        log("nothing to write")
        return
    limit = cfg.get("max_writes_per_pass", 0)
    if limit and len(pending) > limit:
        log(f"gentle mode: writing {limit} of {len(pending)} this pass")
        pending = pending[:limit]
    name_tids = {}
    for tid in pending:
        for name in state["classified"][tid]:
            name_tids.setdefault(name, []).append(tid)
    log(f"writing {len(pending)} track(s) across {len(name_tids)} playlist(s)")

    pl_index = {pl.name: pl.id for pl in user_playlists(session)}
    failed = set()
    for name, tids in sorted(name_tids.items()):
        try:
            plid = ensure_playlist(session, state, name, pl_index)
            n = bulk_add(session, plid, tids, cfg)
            save_state(state)
            if n:
                log(f"  +{n} -> {name}")
        except Exception as e:
            log(f"  playlist '{name}' failed: {e}")
            failed.add(name)

    # a track is done only if every one of its target playlists succeeded
    for tid in pending:
        if not (set(state["classified"][tid]) & failed):
            state["_processed_set"].add(tid)
            state["classified"].pop(tid, None)
    save_state(state)
    log("pass complete")


def cmd_run():
    cfg = load_config()
    session = restore_session()
    if not session:
        log("Not logged in. Run: python organizer.py login")
        sys.exit(1)
    log("organizer started; poll every", cfg["poll_interval_seconds"], "s")
    while True:
        try:
            session = restore_session() or session
            state = load_state()
            one_pass(cfg, session, state)
        except Exception as e:
            log("pass error:", repr(e))
        time.sleep(cfg["poll_interval_seconds"])


def cmd_sample():
    """Dry run: classify the first N tracks and print target playlists. No writes."""
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 8
    cfg = load_config()
    session = restore_session()
    if not session:
        log("Not logged in.")
        sys.exit(1)
    tracks = all_playlist_tracks(session, str(cfg["main_playlist_id"]))[:n]
    payload = [dict(index=j, **track_meta(t)) for j, t in enumerate(tracks)]
    existing = {pl.name for pl in user_playlists(session)}
    results = gemini_classify(cfg, payload, existing)
    print(f"\nDRY RUN — {len(tracks)} tracks (no changes made):\n")
    for j, t in enumerate(tracks):
        m = track_meta(t)
        names = targets_for(cfg, results.get(j))
        print(f"  {m['artist']} — {t.name}")
        print(f"      -> {', '.join(names)}\n")


def cmd_selftest():
    """Validate the Tidal write path: create a temp playlist, add a track,
    verify, then delete it."""
    cfg = load_config()
    session = restore_session()
    tid = all_playlist_tracks(session, str(cfg["main_playlist_id"]))[0].id
    pl = session.user.create_playlist("zz-organizer-selftest", "temp — safe to delete")
    print("created:", pl.id)
    pl.add([tid])
    print("added track:", tid)
    ids = playlist_track_ids(session, pl.id)
    print("playlist now has", len(ids), "track(s); contains our track:", str(tid) in ids)
    pl.delete()
    print("deleted temp playlist — write path OK")


def cmd_once():
    cfg = load_config()
    session = restore_session()
    if not session:
        log("Not logged in. Run: python organizer.py login")
        sys.exit(1)
    one_pass(cfg, session, load_state())


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    {"login": cmd_login, "list-playlists": cmd_list_playlists, "sample": cmd_sample,
     "run": cmd_run, "selftest": cmd_selftest, "once": cmd_once}.get(cmd, cmd_run)()
