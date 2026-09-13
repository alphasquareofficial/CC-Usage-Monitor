import os
import json
import datetime
import tempfile
import time
from claude_monitor_daemon import (
    parse_session_stats, UsageIndex, weigh, cost_of, WEEK_SECONDS,
)


def write_jsonl(lines, mtime=None):
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.jsonl') as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")
        path = f.name
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def assistant(msg_id, request_id, usage, model="claude-opus-5", timestamp=None):
    entry = {
        "type": "assistant",
        "requestId": request_id,
        "message": {"id": msg_id, "model": model, "usage": usage},
    }
    if timestamp:
        entry["timestamp"] = timestamp
    return entry


def test_counts_tokens_and_cost():
    path = write_jsonl([
        assistant("msg_1", "req_1", {"input_tokens": 1000000, "output_tokens": 500000}),
        {"other": "data"},
    ])
    try:
        stats = parse_session_stats(path)
        # weighted mode: 1M input x1.0 + 0.5M output x5.0
        assert stats["weekly_total"] == 3500000
        # Opus 5 pricing: 1M x $5 + 0.5M x $25
        assert stats["weekly_cost"] == 17.5
        assert stats["active"] is True
    finally:
        os.remove(path)


def test_deduplicates_repeated_streaming_records():
    """Claude Code logs one line per streaming update, all carrying the same
    cumulative usage. Only the first occurrence may count."""
    usage = {"input_tokens": 100, "output_tokens": 200}
    path = write_jsonl([
        assistant("msg_1", "req_1", usage),
        assistant("msg_1", "req_1", usage),
        assistant("msg_1", "req_1", usage),
        assistant("msg_2", "req_2", usage),
    ])
    try:
        stats = parse_session_stats(path)
        # Two distinct messages, not four records.
        assert stats["weekly_total"] == 2 * weigh([100, 0, 0, 200], (1.0, 1.25, 0.1, 5.0))
    finally:
        os.remove(path)


def test_ignores_synthetic_messages():
    path = write_jsonl([
        assistant("msg_1", "req_1", {"input_tokens": 100, "output_tokens": 100}),
        assistant("syn", "req_2", {"input_tokens": 9999, "output_tokens": 9999},
                  model="<synthetic>"),
    ])
    try:
        stats = parse_session_stats(path)
        assert stats["weekly_total"] == weigh([100, 0, 0, 100], (1.0, 1.25, 0.1, 5.0))
    finally:
        os.remove(path)


def test_cache_reads_are_weighted_not_ignored():
    path = write_jsonl([
        assistant("msg_1", "req_1", {
            "input_tokens": 0,
            "cache_creation_input_tokens": 1000,
            "cache_read_input_tokens": 100000,
            "output_tokens": 0,
        }),
    ])
    try:
        stats = parse_session_stats(path)
        # 1000 x 1.25 + 100000 x 0.1
        assert stats["weekly_total"] == 11250
    finally:
        os.remove(path)


def test_inactive_when_logs_are_stale():
    old_time = time.time() - (20 * 60)
    path = write_jsonl([
        assistant("msg_1", "req_1", {"input_tokens": 100, "output_tokens": 50}),
    ], mtime=old_time)
    try:
        stats = parse_session_stats(path)
        assert stats["active"] is False
    finally:
        os.remove(path)


def test_five_hour_block_excludes_previous_block():
    now = time.time()
    index = UsageIndex()
    # One call 8 hours ago (a closed block) and one 10 minutes ago (the open one).
    index.events = [
        (now - 8 * 3600, "claude-opus-5", 1000, 0, 0, 0),
        (now - 600, "claude-opus-5", 500, 0, 0, 0),
    ]
    index.last_file_mtime = now
    stats = index.snapshot({"limit_5h": 1000000, "limit_weekly": 1000000})
    assert stats["5h_total"] == 500
    assert stats["weekly_total"] == 1500
    assert stats["reset_in"] > 0


def test_incremental_tail_picks_up_appended_lines():
    path = write_jsonl([
        assistant("msg_1", "req_1", {"input_tokens": 100, "output_tokens": 0}),
    ])
    try:
        index = UsageIndex()
        index.refresh_paths([path])
        assert index.snapshot()["weekly_total"] == 100

        with open(path, "a") as f:
            f.write(json.dumps(
                assistant("msg_2", "req_2", {"input_tokens": 50, "output_tokens": 0})
            ) + "\n")

        index.refresh_paths([path])
        assert index.snapshot()["weekly_total"] == 150
        assert len(index.events) == 2
    finally:
        os.remove(path)


def test_partial_trailing_line_is_held_back():
    path = write_jsonl([
        assistant("msg_1", "req_1", {"input_tokens": 100, "output_tokens": 0}),
    ])
    try:
        # Simulate Claude Code mid-write: a truncated JSON line at the end.
        with open(path, "a") as f:
            f.write('{"type":"assistant","message":{"id":"msg_2","usa')

        index = UsageIndex()
        index.refresh_paths([path])
        assert index.snapshot()["weekly_total"] == 100

        # Complete the line; the next pass must pick it up in full.
        with open(path, "a") as f:
            f.write('ge":{"input_tokens":70,"output_tokens":0}},"requestId":"req_2"}\n')

        index.refresh_paths([path])
        assert index.snapshot()["weekly_total"] == 170
    finally:
        os.remove(path)


def test_opus_and_sonnet_priced_separately():
    assert cost_of("claude-opus-5", 1_000_000, 0, 0, 0) == 5.0
    assert cost_of("claude-sonnet-5", 1_000_000, 0, 0, 0) == 2.0
    assert cost_of("claude-opus-5", 0, 0, 0, 1_000_000) == 25.0
    # cache write 1.25x, cache read 0.1x of input price
    assert cost_of("claude-opus-5", 0, 1_000_000, 0, 0) == 6.25
    assert cost_of("claude-opus-5", 0, 0, 1_000_000, 0) == 0.5


def test_weekly_window_is_anchored_not_rolling():
    """The weekly limit resets on a fixed 7-day cadence, so a call made before
    the reset must not count toward the window after it."""
    index = UsageIndex()
    anchor = "2026-09-20T06:30:00+05:30"
    reset = datetime.datetime.fromisoformat(anchor).timestamp()
    config = {"weekly_reset": anchor}

    # Just after a reset, the window starts at that reset instant.
    now = reset + 3600
    assert index.current_week_start(config, now) == reset

    # Just before it, the window is the previous one.
    now = reset - 3600
    assert index.current_week_start(config, now) == reset - WEEK_SECONDS

    # The cadence repeats forward too, without needing a future anchor.
    now = reset + 3 * WEEK_SECONDS + 100
    assert index.current_week_start(config, now) == reset + 3 * WEEK_SECONDS


def test_weekly_window_falls_back_to_rolling_without_anchor():
    index = UsageIndex()
    now = time.time()
    assert index.current_week_start({}, now) == now - WEEK_SECONDS
    assert index.current_week_start({"weekly_reset": "nonsense"}, now) == now - WEEK_SECONDS


def test_usage_before_weekly_reset_is_excluded():
    """A call made an hour before the reset must not count toward the new week."""
    anchor = "2026-09-20T06:30:00+05:30"
    reset = datetime.datetime.fromisoformat(anchor).timestamp()
    index = UsageIndex()
    index.events = [
        (reset - 3600, "claude-opus-5", 1000, 0, 0, 0),   # previous week
        (reset + 3600, "claude-opus-5", 250, 0, 0, 0),    # current week
    ]
    index.last_file_mtime = reset + 3600
    week_start = index.current_week_start({"weekly_reset": anchor}, reset + 7200)
    counted = sum(i for ts, _m, i, _cc, _cr, _o in index.events if ts >= week_start)
    assert counted == 250
    # A rolling window would wrongly sweep in the previous week's 1000.
    assert index.current_week_start({}, reset + 7200) < reset - 3600


def test_api_utilization_overrides_local_estimate(monkeypatch):
    """When the live API is reachable its percentages win outright -- the local
    token estimate and the configured limits must not influence the bars."""
    import claude_monitor_daemon as d
    now = time.time()
    index = UsageIndex()
    index.events = [(now - 600, "claude-opus-5", 999999999, 0, 0, 0)]
    index.last_file_mtime = now

    monkeypatch.setattr(d.API, "get", lambda: {
        "pct_5h": 52.0, "pct_weekly": 5.0,
        "reset_5h": now + 3660, "reset_weekly": now + 4 * 86400,
        "fetched_at": now,
    })
    stats = index.snapshot({"limit_5h": 1, "limit_weekly": 1})

    assert stats["source"] == "api"
    assert stats["pct_5h"] == 52          # not 99999999900
    assert stats["pct_weekly"] == 5
    assert stats["progress_5h"] == 0.52
    assert stats["str_5h"].startswith("52% - 1h")
    assert stats["str_weekly"].startswith("5% - 3d")


def test_falls_back_to_local_estimate_when_api_down(monkeypatch):
    import claude_monitor_daemon as d
    now = time.time()
    index = UsageIndex()
    index.events = [(now - 600, "claude-opus-5", 500, 0, 0, 0)]
    index.last_file_mtime = now

    monkeypatch.setattr(d.API, "get", lambda: None)
    stats = index.snapshot({"limit_5h": 1000, "limit_weekly": 10000})

    assert stats["source"] == "local"
    assert stats["pct_5h"] == 50
    assert stats["pct_weekly"] == 5


def test_stale_api_reading_is_not_trusted(monkeypatch):
    import claude_monitor_daemon as d
    api = d.UsageAPI()
    api.data = {"pct_5h": 1.0, "pct_weekly": 1.0, "reset_5h": None,
                "reset_weekly": None, "fetched_at": time.time()}
    assert api.get() is not None
    api.data["fetched_at"] = time.time() - (d.API_STALE_AFTER + 1)
    assert api.get() is None


def test_learn_limits_infers_limit_from_api_percentage(monkeypatch, tmp_path):
    import claude_monitor_daemon as d
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"limit_5h": 1, "limit_weekly": 1}))
    monkeypatch.setattr(d.os.path, "expanduser",
                        lambda p: str(cfg) if p == "~/.claude_monitor.json" else p)

    d.learn_limits({"5h_total": 4_000_000, "weekly_total": 20_000_000},
                   {"pct_5h": 40.0, "pct_weekly": 25.0})

    written = json.loads(cfg.read_text())
    assert written["limit_5h"] == 10_000_000     # 4M at 40%
    assert written["limit_weekly"] == 80_000_000  # 20M at 25%


def test_learn_limits_ignores_noisy_low_percentages(monkeypatch, tmp_path):
    import claude_monitor_daemon as d
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"limit_5h": 123, "limit_weekly": 456}))
    monkeypatch.setattr(d.os.path, "expanduser",
                        lambda p: str(cfg) if p == "~/.claude_monitor.json" else p)

    # 4% rounds coarsely enough that the implied limit would be junk.
    d.learn_limits({"5h_total": 4_000_000, "weekly_total": 4_000_000},
                   {"pct_5h": 4.0, "pct_weekly": 4.0})

    assert json.loads(cfg.read_text()) == {"limit_5h": 123, "limit_weekly": 456}
