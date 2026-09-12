import os
import json
import tempfile
import time
from claude_monitor_daemon import parse_session_stats

def test_parse_session_stats():
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.jsonl') as f:
        # Mock some claude code jsonl
        f.write(json.dumps({"message": {"usage": {"input_tokens": 1000000, "output_tokens": 500000}}}) + "\n")
        f.write(json.dumps({"other": "data"}) + "\n")
        temp_path = f.name
        
    try:
        stats = parse_session_stats(temp_path)
        assert stats["total"] == 1500000
        # Cost: 1M input = $3.0, 0.5M output = $7.5 => total 10.5
        assert stats["cost"] == 10.5
        assert stats["active"] == True
    finally:
        os.remove(temp_path)

def test_parse_session_stats_inactive():
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.jsonl') as f:
        f.write(json.dumps({"message": {"usage": {"input_tokens": 100, "output_tokens": 50}}}) + "\n")
        temp_path = f.name
        
    try:
        # Fake old modification time
        old_time = time.time() - (20 * 60)
        os.utime(temp_path, (old_time, old_time))
        
        stats = parse_session_stats(temp_path)
        assert stats["total"] == 150
        assert stats["active"] == False
    finally:
        os.remove(temp_path)
