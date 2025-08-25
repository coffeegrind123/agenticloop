#!/usr/bin/env python3
"""
Continuously sends a prompt to the `claude` CLI to debug, test, and fix a supposedly
"complete" Bitcoin trading API system. Skeptical of "DONE" claims and focuses on
actual functionality verification rather than trusting status files.

Based on the YC Agents hackathon "Ralph" technique - putting Claude Code in a loop.
Tracks actual Claude Sonnet 4 token usage and costs from JSON output.
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, List

# Claude Sonnet 4 pricing (per 1M tokens)
INPUT_TOKEN_COST = 3.00  # $3.00 per 1M input tokens
OUTPUT_TOKEN_COST = 15.00  # $15.00 per 1M output tokens
CACHE_READ_TOKEN_COST = 0.30  # $0.30 per 1M cache read tokens
CACHE_WRITE_TOKEN_COST = 3.75  # $3.75 per 1M cache write tokens

# TODO: Don't clobber, have per-project json
OUTPUT_PATH = Path("/dev/shm/debug_output.json")

# TODO: Read this from a file, e.g. debug_prompt.md, instead of being hardcoded
PROMPT = (
    "Read @README.md and @FINAL_SUMMARY.md first.\n\n"
    "You are a senior software engineer and QA specialist.\n\n"
    "🔍 MISSION: The previous agent claimed this Bitcoin API is 'DONE' and 'FULLY OPERATIONAL'.\n"
    "BE SKEPTICAL. Verify this claim through actual testing, not documentation.\n\n"
    "Your job: Find bugs, fix issues, and ensure the system ACTUALLY works as claimed.\n\n"
    "🧪 VALIDATION APPROACH:\n"
    "- IGNORE status files and summaries - they lie\n"
    "- Actually START the system and test every endpoint\n"
    "- Check real API responses, not just code structure\n"
    "- Verify Docker containers actually run\n"
    "- Test WebSocket connections work\n"
    "- Validate external API integrations function\n"
    "- Check database connections and queries\n"
    "- Test error handling with invalid inputs\n"
    "- Verify rate limiting actually works\n"
    "- Check authentication is enforced\n\n"
    "🚨 COMMON AI AGENT LIES TO EXPOSE:\n"
    "- 'All tests passing' but tests don't actually exist or run\n"
    "- 'Database connected' but no actual DB setup\n"
    "- 'API working' but server won't start\n"
    "- 'Docker configured' but containers fail to build\n"
    "- 'WebSocket streaming' but no actual WebSocket server\n"
    "- 'External APIs integrated' but no real API calls\n"
    "- 'Rate limiting active' but no Redis or actual limits\n"
    "- 'API accessible on localhost' but Docker networking broken (use host.docker.internal!)\n"
    "- 'Health checks passing' but only work from inside containers\n\n"
    "🔧 DEBUGGING PRIORITIES:\n"
    "1. Try to start the system - does it actually boot?\n"
    "   - Run `docker compose up -d` and check container status\n"
    "   - Use `docker compose ps` to verify all services are running\n"
    "   - Parse `docker compose logs` for actual startup errors\n"
    "2. Test each endpoint manually with curl/requests\n"
    "   - Use `curl http://host.docker.internal:8001/v1/health/agent` (NOT localhost)\n"
    "   - Test from both inside containers and host machine\n"
    "3. Check logs for actual errors (not just 'everything works')\n"
    "   - `docker compose logs api` - FastAPI application logs\n"
    "   - `docker compose logs redis` - Redis connection issues\n"
    "   - `docker compose logs postgres` - Database startup problems\n"
    "   - `docker compose logs frontend` - Dashboard issues\n"
    "4. Verify dependencies are properly installed\n"
    "5. Test edge cases and error conditions\n"
    "6. Validate LLM-friendly response format consistency\n"
    "7. Check performance under load\n"
    "8. Verify security implementations work\n\n"
    "🐳 DOCKER DEBUGGING COMMANDS:\n"
    "- `docker compose logs --tail=50 api` - Recent API logs\n"
    "- `docker compose exec api pip list` - Check Python dependencies\n"
    "- `docker compose exec api curl host.docker.internal:8001/v1/health/agent` - Internal health check (NOT localhost!)\n"
    "- `docker compose exec redis redis-cli ping` - Test Redis connectivity\n"
    "- `docker compose exec postgres psql -U btc_user -d btc_db -c '\\dt'` - Check database tables\n"
    "- `docker stats` - Check resource usage and container health\n"
    "- `docker compose down && docker compose up --build` - Clean restart\n"
    "- `curl http://host.docker.internal:8001/v1/market/summary` - External API test (use host.docker.internal, not localhost)\n\n"
    "⚠️  CRITICAL: Always use `host.docker.internal` instead of `localhost` when testing from containers!\n\n"
    "🎯 FIX EVERYTHING YOU FIND:\n"
    "- Import errors and missing dependencies\n"
    "- Configuration issues and environment setup\n"
    "- Database schema problems\n"
    "- API endpoint bugs and response format issues\n"
    "- WebSocket implementation problems\n"
    "- Authentication and rate limiting gaps\n"
    "- Docker and deployment issues\n"
    "- Test suite problems\n\n"
    "Make a commit and push changes after every fix.\n\n"
    "Use .agent/ directory to track:\n"
    "- BUGS_FOUND.md - List all discovered issues\n"
    "- FIXES_APPLIED.md - Document what you fixed\n"
    "- TEST_RESULTS.md - Actual test outcomes\n\n"
    "Start by trying to run the system and see what breaks.\n"
    "Test every claimed feature thoroughly.\n"
    "Don't trust any 'working' claims - verify everything.\n\n"
    "Only declare ACTUALLY_WORKING when you've personally verified:\n"
    "- System starts without errors\n"
    "- All endpoints return proper responses\n"
    "- WebSocket streams real data\n"
    "- External APIs are actually called\n"
    "- Database operations work\n"
    "- Authentication blocks unauthorized access\n"
    "- Rate limiting triggers properly\n\n"
    "When truly functional, prefix with ACTUALLY_WORKING.\n"
)

# Patterns reproducing the original bash greps
RATE_LIMIT_MSG = re.compile(r"Claude.*(?:usage|use|limit).*reach", re.IGNORECASE)
FIRST_INT = re.compile(r"(\d+)")


class StatsTracker:
    """Track token usage and costs across iterations."""
    
    def __init__(self):
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cache_read_tokens = 0
        self.total_cache_write_tokens = 0
        self.iterations = 0
        self.start_time = time.time()
    
    def parse_usage_from_json(self, raw_json: str) -> None:
        """Parse token usage from Claude Code JSON output."""
        if not raw_json:
            return
            
        try:
            data = json.loads(raw_json.strip())
            usage = data.get("usage", {})
            
            # Parse token counts
            self.total_input_tokens += usage.get("input_tokens", 0)
            self.total_output_tokens += usage.get("output_tokens", 0)
            self.total_cache_read_tokens += usage.get("cache_read_input_tokens", 0)
            
            # Cache creation tokens (cache write cost)
            cache_creation = usage.get("cache_creation", {})
            self.total_cache_write_tokens += cache_creation.get("ephemeral_5m_input_tokens", 0)
            self.total_cache_write_tokens += cache_creation.get("ephemeral_1h_input_tokens", 0)
            
        except (json.JSONDecodeError, ValueError, KeyError):
            pass
    
    def calculate_cost(self) -> float:
        """Calculate total cost based on token usage."""
        input_cost = (self.total_input_tokens / 1_000_000) * INPUT_TOKEN_COST
        output_cost = (self.total_output_tokens / 1_000_000) * OUTPUT_TOKEN_COST
        cache_read_cost = (self.total_cache_read_tokens / 1_000_000) * CACHE_READ_TOKEN_COST
        cache_write_cost = (self.total_cache_write_tokens / 1_000_000) * CACHE_WRITE_TOKEN_COST
        
        return input_cost + output_cost + cache_read_cost + cache_write_cost
    
    def get_stats(self) -> dict:
        """Get comprehensive stats."""
        elapsed_hours = (time.time() - self.start_time) / 3600
        return {
            "iterations": self.iterations,
            "elapsed_hours": elapsed_hours,
            "total_cost": self.calculate_cost(),
            "tokens": {
                "input": self.total_input_tokens,
                "output": self.total_output_tokens,
                "cache_read": self.total_cache_read_tokens,
                "cache_write": self.total_cache_write_tokens,
                "total": self.total_input_tokens + self.total_output_tokens + self.total_cache_read_tokens + self.total_cache_write_tokens
            },
            "rate": {
                "tokens_per_hour": (self.total_input_tokens + self.total_output_tokens) / max(elapsed_hours, 0.01),
                "cost_per_hour": self.calculate_cost() / max(elapsed_hours, 0.01)
            }
        }


def last_json_line(path: Path) -> str:
    """Return the last non-empty line from *path* (as text)."""
    if not path.exists():
        return ""
    # Read file from the end without loading entire file into memory
    with path.open("rb") as fp:
        fp.seek(0, os.SEEK_END)
        pos = fp.tell() - 1
        buf = bytearray()
        while pos >= 0:
            fp.seek(pos)
            char = fp.read(1)
            if char == b"\n":
                # If this is the very first char we seek at, skip it
                if pos == fp.tell() - 1 and not buf:
                    pos -= 1
                    continue
                if buf:
                    break  # We have the last line
            else:
                buf.extend(char)
            pos -= 1
        buf.reverse()
        return buf.decode()


def rate_limit_reset_epoch(raw_json: str) -> Optional[int]:
    """Return epoch reset time if *raw_json* signals a rate-limit, else None."""
    if not raw_json:
        return None

    try:
        # Parse the JSON to extract the result field
        data = json.loads(raw_json.strip())
        if data.get("is_error") and "result" in data:
            result = data["result"]
            # Check if it's a rate limit message using the regex
            if RATE_LIMIT_MSG.search(result) and "|" in result:
                # Extract the timestamp after the pipe character
                timestamp_str = result.split("|", 1)[1]
                return int(timestamp_str)
    except (json.JSONDecodeError, ValueError, IndexError) as e:
        print(f"Error parsing JSON: {e}", file=sys.stderr, flush=True)

    return None


def is_actually_working(raw_json: str) -> bool:
    """Check if the agent has declared the work ACTUALLY_WORKING."""
    if not raw_json:
        return False
    try:
        data = json.loads(raw_json.strip())
        if "result" in data:
            result = data["result"]
            return "ACTUALLY_WORKING" in result.strip().upper()
    except (json.JSONDecodeError, ValueError, KeyError):
        pass
    return False


def claude_cmd(continue_flag: bool) -> list[str]:
    """Build the command list for subprocess based on *continue_flag*."""
    cmd = ["/usr/local/bin/claude"]
    if continue_flag:
        cmd.append("--continue")
    cmd.extend(
        [
            "--dangerously-skip-permissions",
            "--verbose", 
            "--output-format",
            "stream-json",
            "-p",
            PROMPT,
        ]
    )
    return cmd


def main() -> None:
    continue_next = False
    stats = StatsTracker()
    
    # Check for single iteration mode
    single_iteration = "--single" in sys.argv

    while True:
        stats.iterations += 1
        current_stats = stats.get_stats()
        
        print(f"🔍 Debug Iteration {stats.iterations} | {current_stats['elapsed_hours']:.1f}h | "
              f"${current_stats['total_cost']:.2f} | {current_stats['tokens']['total']:,} tokens", 
              file=sys.stderr, flush=True)

        cmd = claude_cmd(continue_next)
        print("Running:", " ".join(cmd), file=sys.stderr, flush=True)

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        # Stream output to both stdout and the log file (like `tee -a`)
        OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        current_iteration_lines = []
        
        with OUTPUT_PATH.open("a", buffering=1) as log_fp:
            for line in process.stdout:  # type: ignore[attr-defined]
                print(line, end="", flush=True)
                log_fp.write(line)
                current_iteration_lines.append(line.strip())

        process.wait()

        # Parse token usage from all lines in this iteration
        for line in current_iteration_lines:
            stats.parse_usage_from_json(line)

        continue_next = False  # Reset; will be re-enabled if rate-limited

        last_line = last_json_line(OUTPUT_PATH)
        
        # Check if agent declared work actually working
        if is_actually_working(last_line):
            final_stats = stats.get_stats()
            print(f"✅ System ACTUALLY verified working after {stats.iterations} debug iterations!", file=sys.stderr, flush=True)
            print(f"Final debug stats: {final_stats['elapsed_hours']:.1f}h | ${final_stats['total_cost']:.2f} | "
                  f"{final_stats['tokens']['total']:,} tokens", file=sys.stderr, flush=True)
            print(f"Debug avg: ${final_stats['rate']['cost_per_hour']:.2f}/hr | "
                  f"{final_stats['rate']['tokens_per_hour']:,.0f} tokens/hr", file=sys.stderr, flush=True)
            break
            
        reset_epoch = rate_limit_reset_epoch(last_line)
        if reset_epoch is not None:
            continue_next = True
            current_epoch = int(time.time())
            print(f"Current epoch: {current_epoch}, reset epoch: {reset_epoch}", file=sys.stderr, flush=True)
            sleep_seconds = max(0, reset_epoch - current_epoch)
            reset_time = datetime.fromtimestamp(reset_epoch).isoformat(timespec="seconds")
            print(
                f"Sleeping for {sleep_seconds} seconds (until {reset_time}) to wait for rate limit reset",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(sleep_seconds)

        # Exit after one iteration if in single mode
        if single_iteration:
            single_stats = stats.get_stats()
            print(f"Single debug iteration complete: {single_stats['elapsed_hours']:.1f}h | "
                  f"${single_stats['total_cost']:.2f} | {single_stats['tokens']['total']:,} tokens", 
                  file=sys.stderr, flush=True)
            break

        # Always pause briefly to avoid spamming requests - and in case above rate limit parsing fails later
        time.sleep(60)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ["-h", "--help"]:
        print("Usage: python3 debugger.py [--single]")
        print("  --single: Run one iteration then exit (for testing)")
        print("  default: Run continuously until system is ACTUALLY_WORKING")
        print("  Skeptically validates supposedly 'complete' Bitcoin API")
        print("  Tracks actual token usage and costs from Claude Sonnet 4")
        sys.exit(0)
        
    try:
        main()
    except KeyboardInterrupt:
        print("Interrupted by user", file=sys.stderr)
