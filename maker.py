#!/usr/bin/env python3
"""
Continuously sends a prompt to the `claude` CLI to build a Bitcoin trading API system,
stores the streamed JSON responses to a file, and respects any reported rate-limit reset
time by sleeping until the limit expires. Includes early stopping when agent declares 
work complete, accurate cost tracking based on token usage, and single-iteration mode for testing.

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
OUTPUT_PATH = Path("/dev/shm/output.json")

# TODO: Read this from a file, e.g. bitcoin_prompt.md, instead of being hardcoded
PROMPT = (
    "Read @PRD.md and @README.md first.\n\n"
    "You are a senior software engineer building a production-ready Bitcoin trading API system.\n\n"
    "🎯 CRITICAL: This API will be consumed by another Claude Code agent running in a continuous loop (similar to this script).\n"
    "Design every endpoint specifically for LLM consumption with clear, predictable responses.\n\n"
    "Your goal: Build a comprehensive Bitcoin trading API with real-time market data, optimized for LLM consumption.\n\n"
    "🎯 CORE API DESIGN PRINCIPLES (follow religiously):\n"
    "- GOOD APIs ARE BORING: Make it so familiar that users know how to use it before reading docs\n"
    "- WE DO NOT BREAK USERSPACE: Never make breaking changes. Only additive changes allowed.\n"
    "- Simple API key authentication (not just OAuth) - many users aren't professional engineers\n"
    "- Idempotency keys for all action requests (trades, orders) to make retries safe\n"
    "- Cursor-based pagination for large datasets (trade history, market data)\n"
    "- Rate limiting with clear X-Limit-Remaining headers\n"
    "- Make expensive fields optional with include parameters\n"
    "- Focus on the underlying value (fast Bitcoin data) not API elegance\n"
    "- Design around your core resources: markets, prices, trades, portfolios\n\n"
    "🤖 LLM AGENT OPTIMIZATION:\n"
    "- Every response must include 'status' and 'timestamp' fields\n"
    "- Error messages must be actionable and specific\n"
    "- Include 'confidence' scores for trading signals\n"
    "- Provide 'next_action_suggestions' in responses when relevant\n"
    "- All monetary values in consistent units (satoshis)\n"
    "- Include 'rate_limit_remaining' in every response\n"
    "- Endpoints should return everything an LLM needs in a single call\n"
    "- No complex nested objects - keep JSON flat and predictable\n\n"
    "Requirements:\n"
    "- Real-time Bitcoin market data from multiple free APIs (CoinGecko, CoinDesk, Binance public, etc.)\n"
    "- Use pydoll or similar for scraping additional data sources when needed\n"
    "- FastAPI backend with async endpoints optimized for speed\n"
    "- Bitcoin testnet integration (NO real money/bitcoin)\n"
    "- Beautiful real-time dashboard tracking all agent activities\n"
    "- WebSocket support for live data streaming\n"
    "- Rate limiting, caching, and error handling for reliability\n"
    "- LLM-friendly JSON responses with clear schemas\n"
    "- Comprehensive logging and monitoring\n"
    "- Docker containerization for easy deployment\n\n"
    "API Design Standards:\n"
    "- REST endpoints following /v1/resource pattern\n"
    "- Consistent error responses with proper HTTP status codes\n"
    "- Include next_page fields in paginated responses\n"
    "- Support includes parameter for optional expensive data\n"
    "- Idempotency-Key header support for POST/PUT operations\n"
    "- Rate limit headers: X-RateLimit-Limit, X-RateLimit-Remaining, Retry-After\n"
    "- Simple API key auth via Authorization: Bearer <key> header\n"
    "- OpenAPI/Swagger documentation with examples\n\n"
    "Key LLM-Friendly Endpoints to Implement:\n"
    "1. GET /v1/market/summary - Single call for all market data an LLM needs\n"
    "2. GET /v1/signals/latest - Trading signals with confidence scores\n"
    "3. POST /v1/trades/simulate - Testnet trade execution with clear results\n"
    "4. GET /v1/portfolio/status - Complete portfolio state in one response\n"
    "5. GET /v1/health/agent - Endpoint specifically for agent health checks\n"
    "6. WebSocket /v1/stream/live - Real-time feed for continuous monitoring\n\n"
    "Architecture:\n"
    "- FastAPI server with async/await\n"
    "- Redis for caching, rate limiting, and idempotency keys\n"
    "- SQLite/PostgreSQL for trade history\n"
    "- React dashboard with real-time updates\n"
    "- Background tasks for data collection\n\n"
    "Make a commit and push changes after every single file edit.\n\n"
    "Use .agent/ directory as scratchpad for plans and todo lists.\n"
    "NEVER push to main branch - always use feature branches and PRs.\n"
    "Write comprehensive tests FIRST, then implement to pass the tests.\n"
    "Add security reviews to .agent/SECURITY_CHECKLIST.md - no default passwords, proper auth, input validation.\n"
    "Set up proper CI/CD pipeline to prevent vulnerable code from being recommitted.\n\n"
    "Run tests often, don't assume your code is working.\n"
    "Build incrementally - start with basic API, then add features.\n"
    "Focus on speed and reliability - this will be used by trading algorithms.\n"
    "Make conventional git commits between major changes.\n"
    "Be self-critical, terse, clear, and concise.\n"
    "Add comprehensive error handling and logging.\n"
    "Remember: good APIs are tools users barely think about.\n"
    "Design for Claude Code agents that will call this API in continuous loops.\n\n"
    "Start by creating the project structure and basic FastAPI server.\n"
    "Then implement market data collection from free APIs.\n"
    "Add the real-time dashboard.\n"
    "Finally, integrate testnet trading capabilities.\n\n"
    "When done, prefix with DONE.\n"
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


def is_done(raw_json: str) -> bool:
    """Check if the agent has declared the work DONE."""
    if not raw_json:
        return False
    try:
        data = json.loads(raw_json.strip())
        if "result" in data:
            result = data["result"]
            return result.strip().upper().startswith("DONE")
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
            #"--timeout", "900",  # 15 minute timeout per iteration
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
        
        print(f"Iteration {stats.iterations} | {current_stats['elapsed_hours']:.1f}h | "
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
        
        # Check if agent declared work done
        if is_done(last_line):
            final_stats = stats.get_stats()
            print(f"Agent completed work after {stats.iterations} iterations!", file=sys.stderr, flush=True)
            print(f"Final stats: {final_stats['elapsed_hours']:.1f}h | ${final_stats['total_cost']:.2f} | "
                  f"{final_stats['tokens']['total']:,} tokens", file=sys.stderr, flush=True)
            print(f"Avg: ${final_stats['rate']['cost_per_hour']:.2f}/hr | "
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
            print(f"Single iteration complete: {single_stats['elapsed_hours']:.1f}h | "
                  f"${single_stats['total_cost']:.2f} | {single_stats['tokens']['total']:,} tokens", 
                  file=sys.stderr, flush=True)
            break

        # Always pause briefly to avoid spamming requests - and in case above rate limit parsing fails later
        time.sleep(60)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ["-h", "--help"]:
        print("Usage: python3 maker.py [--single]")
        print("  --single: Run one iteration then exit (for testing)")
        print("  default: Run continuously until agent declares DONE")
        print("  Tracks actual token usage and costs from Claude Sonnet 4")
        sys.exit(0)
        
    try:
        main()
    except KeyboardInterrupt:
        print("Interrupted by user", file=sys.stderr)
