"""Token usage read straight off the local transcripts. No API calls.

Claude Code writes one JSONL per session under ~/.claude/projects/<slug>/, where
each `assistant` entry carries `message.usage`. Codex writes rollout JSONL under
~/.codex/sessions/YYYY/MM/DD/, where `token_count` events carry both usage and an
authoritative `rate_limits` snapshot from the server.

Files are parsed incrementally: a cache remembers each file's size and byte
offset, so a refresh only reads what was appended.
"""
import json
import os
import time
from datetime import datetime, timedelta, timezone

from . import paths

BLOCK_HOURS = 5
KEEP_DAYS = 8


def _load_cache() -> dict:
    try:
        with open(paths.USAGE_CACHE, "r", encoding="utf-8") as fh:
            cache = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {"files": {}}
    if not isinstance(cache, dict) or "files" not in cache:
        return {"files": {}}
    return cache


def _save_cache(cache: dict) -> None:
    paths.ensure_data()
    tmp = paths.USAGE_CACHE.with_suffix(".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cache, fh)
        os.replace(tmp, paths.USAGE_CACHE)
    except OSError:
        pass


def _parse_ts(value) -> float:
    """ISO-8601 (Claude) or epoch seconds (Codex) -> epoch float."""
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value:
        return 0.0
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _claude_entries(text: str) -> list:
    """-> [[ts, total, input, output, cache_read, cache_write, model, uid], ...]"""
    out = []
    for line in text.splitlines():
        if '"usage"' not in line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "assistant":
            continue
        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue
        usage = msg.get("usage")
        if not isinstance(usage, dict):
            continue
        inp = int(usage.get("input_tokens") or 0)
        out_tok = int(usage.get("output_tokens") or 0)
        read = int(usage.get("cache_read_input_tokens") or 0)
        write = int(usage.get("cache_creation_input_tokens") or 0)
        out.append(
            [
                _parse_ts(obj.get("timestamp")),
                inp + out_tok + read + write,
                inp,
                out_tok,
                read,
                write,
                msg.get("model") or "",
                str(msg.get("id") or obj.get("requestId") or obj.get("uuid") or ""),
            ]
        )
    return out


def _codex_entries(text: str):
    """-> (entries, latest_rate_limits_or_None). Uses per-request deltas."""
    out, limits = [], None
    for line in text.splitlines():
        if '"token_count"' not in line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = obj.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "token_count":
            continue
        ts = _parse_ts(obj.get("timestamp"))
        if isinstance(payload.get("rate_limits"), dict):
            limits = dict(payload["rate_limits"])
            limits["_ts"] = ts
        info = payload.get("info")
        if not isinstance(info, dict):
            continue
        last = info.get("last_token_usage")
        if not isinstance(last, dict):
            continue
        inp = int(last.get("input_tokens") or 0)
        out_tok = int(last.get("output_tokens") or 0)
        cached = int(last.get("cached_input_tokens") or 0)
        write = int(last.get("cache_write_input_tokens") or 0)
        total = int(last.get("total_tokens") or (inp + out_tok))
        out.append([ts, total, inp, out_tok, cached, write, "", ""])
    return out, limits


def _scan(root, pattern: str, parser, cache: dict, kind: str):
    """Walk transcripts, parsing only bytes appended since the last scan."""
    entries, limits = [], None
    cutoff = time.time() - KEEP_DAYS * 86400
    files = cache.setdefault("files", {})
    seen = set()
    if not root.exists():
        return entries, limits

    for path in root.rglob(pattern):
        try:
            stat = path.stat()
        except OSError:
            continue
        key = kind + "|" + str(path)
        seen.add(key)
        record = files.get(key)
        if stat.st_mtime < cutoff and record is None:
            # Old file never seen before: skip entirely, nothing recent in it.
            files[key] = {"size": stat.st_size, "offset": stat.st_size, "entries": []}
            continue
        if record and record.get("size") == stat.st_size:
            entries.extend(record.get("entries", []))
            if record.get("limits"):
                limits = _newer(limits, record["limits"])
            continue

        offset = record.get("offset", 0) if record and stat.st_size > record.get("size", 0) else 0
        try:
            with open(path, "rb") as fh:
                fh.seek(offset)
                chunk = fh.read()
        except OSError:
            continue
        cut = chunk.rfind(bytes([10]))
        if cut == -1:
            new_offset, text = offset, ""
        else:
            new_offset, text = offset + cut + 1, chunk[: cut + 1].decode("utf-8", "replace")

        if kind == "codex":
            fresh, file_limits = parser(text)
        else:
            fresh, file_limits = parser(text), None

        prior = record.get("entries", []) if (record and offset) else []
        merged = [e for e in prior + fresh if e[0] >= cutoff]
        stored_limits = file_limits or (record.get("limits") if record else None)
        files[key] = {
            "size": stat.st_size,
            "offset": new_offset,
            "entries": merged,
            "limits": stored_limits,
        }
        entries.extend(merged)
        limits = _newer(limits, stored_limits)

    for key in [k for k in files if k.startswith(kind + "|") and k not in seen]:
        del files[key]
    return entries, limits


def _newer(current, candidate):
    if not candidate:
        return current
    if not current:
        return candidate
    return candidate if candidate.get("_ts", 0) >= current.get("_ts", 0) else current


def _dedupe(entries: list) -> list:
    """Claude replays assistant messages across forks/resumes; keep one each."""
    out, seen = [], set()
    for entry in entries:
        uid = entry[7]
        if uid:
            if uid in seen:
                continue
            seen.add(uid)
        out.append(entry)
    return out


def _active_block(entries: list):
    """ccusage-style 5h block: tokens in the window, and when it resets."""
    if not entries:
        return {"tokens": 0, "resets_at": 0, "started_at": 0}
    ordered = sorted(entries, key=lambda e: e[0])
    span = BLOCK_HOURS * 3600
    start = _floor_hour(ordered[0][0])
    total, previous = 0, ordered[0][0]
    for ts, tokens in ((e[0], e[1]) for e in ordered):
        if ts - start >= span or ts - previous >= span:
            start, total = _floor_hour(ts), 0
        total += tokens
        previous = ts
    now = time.time()
    if now - start >= span:  # the last block already expired
        return {"tokens": 0, "resets_at": 0, "started_at": 0}
    return {"tokens": total, "resets_at": start + span, "started_at": start}


def _floor_hour(ts: float) -> float:
    moment = datetime.fromtimestamp(ts, timezone.utc).replace(minute=0, second=0, microsecond=0)
    return moment.timestamp()


def _daily(entries: list) -> dict:
    """Local-date -> token total."""
    buckets: dict[str, int] = {}
    for entry in entries:
        if not entry[0]:
            continue
        day = datetime.fromtimestamp(entry[0]).strftime("%Y-%m-%d")
        buckets[day] = buckets.get(day, 0) + entry[1]
    return buckets


def latest_codex_limits():
    """Newest rate_limits snapshot, even from a session older than KEEP_DAYS.

    Token totals age out, but a stale percentage is still worth showing (labelled
    with its age) -- it is the only Codex figure that exists between sessions.
    """
    root = paths.CODEX_SESSIONS
    if not root.exists():
        return None
    try:
        recent = sorted(root.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:12]
    except OSError:
        return None
    # The newest session may have died before its first response (a failed or
    # rate-limited turn writes no token_count), so walk back until one has one.
    for path in recent:
        try:
            size = path.stat().st_size
            with open(path, "rb") as fh:
                if size > 512 * 1024:
                    fh.seek(size - 512 * 1024)
                    fh.readline()
                text = fh.read().decode("utf-8", "replace")
        except OSError:
            continue
        _, found = _codex_entries(text)
        if _has_percent(found):
            return found
    return None


def _has_percent(limits) -> bool:
    """A snapshot from a rate-limited turn carries null windows; ignore those."""
    if not isinstance(limits, dict):
        return False
    for slot in ("primary", "secondary"):
        window = limits.get(slot)
        if isinstance(window, dict) and window.get("used_percent") is not None:
            return True
    return False


def collect() -> dict:
    """Full usage snapshot for both tools. Safe to call every few seconds."""
    cache = _load_cache()
    claude_entries, _ = _scan(paths.CLAUDE_PROJECTS, "*.jsonl", _claude_entries, cache, "claude")
    codex_entries, codex_limits = _scan(paths.CODEX_SESSIONS, "*.jsonl", _codex_entries, cache, "codex")
    _save_cache(cache)

    claude_entries = _dedupe(claude_entries)
    today = datetime.now().strftime("%Y-%m-%d")
    claude_daily, codex_daily = _daily(claude_entries), _daily(codex_entries)
    week = [(datetime.now() - timedelta(days=offset)).strftime("%Y-%m-%d") for offset in range(7)]

    return {
        "generated_at": time.time(),
        "claude": {
            "block": _active_block(claude_entries),
            "today": claude_daily.get(today, 0),
            "week": sum(claude_daily.get(day, 0) for day in week),
            "daily": claude_daily,
        },
        "codex": {
            "block": _active_block(codex_entries),
            "today": codex_daily.get(today, 0),
            "week": sum(codex_daily.get(day, 0) for day in week),
            "daily": codex_daily,
            "limits": codex_limits if _has_percent(codex_limits) else latest_codex_limits(),
        },
    }


def human_tokens(count: int) -> str:
    if count >= 1_000_000:
        return "%.1fM" % (count / 1_000_000)
    if count >= 1_000:
        return "%.0fk" % (count / 1_000)
    return str(count)


def human_until(epoch: float) -> str:
    if not epoch:
        return "-"
    remaining = int(epoch - time.time())
    if remaining <= 0:
        return "now"
    if remaining >= 172800:  # past two days, hours stop being useful
        return "%dd" % round(remaining / 86400)
    return "%dh%02dm" % (remaining // 3600, (remaining % 3600) // 60)
