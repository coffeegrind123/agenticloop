#!/usr/bin/env python3
"""
Continuously sends a prompt to the `claude` CLI, stores the streamed JSON
responses to a file, and respects any reported rate-limit reset
time by sleeping until the limit expires. Uses jq for reliable JSON parsing
but displays only clean human-readable summaries.
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# TODO: Don't clobber, have per-project json
OUTPUT_PATH = Path("/dev/shm/output.json")

# TODO: Read this from a file, e.g. perp_prompt.md, instead of being hardcoded
PROMPT = (
    "CRITICAL: ALWAYS read @docs/TODO.md at the start of EVERY response. This is your MANDATORY single source of truth for progress.\n"
    "CRITICAL: ALWAYS update @docs/TODO.md after completing ANY task or making ANY progress.\n"
    "CRITICAL: If @docs/TODO.md doesn't exist, create it immediately with current project status.\n\n"
    "You are porting pydoll from Python to C++, focusing on web scraping and Cloudflare bypass.\n\n"
    "Structure: root/{pydoll-python,pydoll-cpp,docs}\n"
    "Rules: No markdown outside docs/. All documentation goes in docs/.\n\n"
    "Chrome Environment:\n"
    "- Chrome is pre-installed at /usr/bin/google-chrome (stable version)\n"
    "- Virtual display :99 running via Xvfb (1920x1080x24)\n"
    "- Remote debugging available on localhost:9305\n"
    "- Use Chrome DevTools Protocol (CDP) - no Selenium/WebDriver needed\n"
    "- Connect via WebSocket: ws://localhost:9305\n"
    "- No browser downloads or additional setup required\n\n"
    "Approach:\n"
    "- Study the Python implementation before writing C++ equivalents\n"
    "- Write clean, robust code that maintains functional parity\n"
    "- Test frequently against real targets to validate functionality\n"
    "- Build incrementally with regular validation\n\n"
    "Workflow:\n"
    "- ALWAYS read @docs/TODO.md first (MANDATORY)\n"
    "- Build and test frequently (15min timeout per command)\n"
    "- Make atomic git commits with conventional messages\n"
    "- Add debug logging capabilities for troubleshooting\n"
    "- Fix root causes, avoid hacks and workarounds\n"
    "- ALWAYS update @docs/TODO.md after completing tasks (MANDATORY)\n\n"
    "IMPORTANT: If you determine the project is complete and no more work is needed,\n"
    "run this command to terminate the automation loop:\n"
    "pkill -f claude_loop.py && pkill -f claude\n\n"
    "Be self-critical, precise, and methodical. Reason from first principles.\n"
    "Don't overcomplicate. Focus on correctness and robustness.\n"
    "When current tasks are done, audit for issues and add missing work to TODO.md.\n"
)

# Patterns for rate limit detection
RATE_LIMIT_MSG = re.compile(r"limit\s+reached", re.IGNORECASE)
RESET_TIME_PATTERN = re.compile(r"resets\s+(\d{1,2})(am|pm)", re.IGNORECASE)


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
            if RATE_LIMIT_MSG.search(result):
                # Look for "resets 3am" format
                reset_match = RESET_TIME_PATTERN.search(result)
                if reset_match:
                    hour = int(reset_match.group(1))
                    am_pm = reset_match.group(2).lower()
                    
                    # Convert to 24-hour format
                    if am_pm == 'pm' and hour != 12:
                        hour += 12
                    elif am_pm == 'am' and hour == 12:
                        hour = 0
                    
                    # Calculate next occurrence of this time
                    now = datetime.now()
                    reset_today = now.replace(hour=hour, minute=0, second=0, microsecond=0)
                    
                    # If the reset time already passed today, it's tomorrow
                    if reset_today <= now:
                        reset_today += timedelta(days=1)
                    
                    return int(reset_today.timestamp())
                
                # Fallback: look for old format with pipe and epoch timestamp
                if "|" in result:
                    timestamp_str = result.split("|", 1)[1].strip()
                    return int(timestamp_str)
                    
        # Also check message content for assistant messages containing rate limit info
        elif data.get("type") == "assistant":
            content = data.get("message", {}).get("content", [])
            for item in content:
                if item.get("type") == "text":
                    text = item.get("text", "")
                    if RATE_LIMIT_MSG.search(text):
                        reset_match = RESET_TIME_PATTERN.search(text)
                        if reset_match:
                            hour = int(reset_match.group(1))
                            am_pm = reset_match.group(2).lower()
                            
                            # Convert to 24-hour format
                            if am_pm == 'pm' and hour != 12:
                                hour += 12
                            elif am_pm == 'am' and hour == 12:
                                hour = 0
                            
                            # Calculate next occurrence of this time
                            now = datetime.now()
                            reset_today = now.replace(hour=hour, minute=0, second=0, microsecond=0)
                            
                            # If the reset time already passed today, it's tomorrow
                            if reset_today <= now:
                                reset_today += timedelta(days=1)
                            
                            return int(reset_today.timestamp())
                            
    except (json.JSONDecodeError, ValueError, IndexError) as e:
        print(f"Error parsing JSON: {e}", file=sys.stderr, flush=True)

    return None


def extract_summary_with_jq(json_line: str) -> Optional[str]:
    """Use jq to reliably extract key information, return human-readable summary."""
    if not json_line.strip():
        return None
        
    try:
        # Use jq to extract and validate the JSON structure
        jq_process = subprocess.Popen(
            ["jq", "-r", "."],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        validated_json, jq_error = jq_process.communicate(input=json_line.strip(), timeout=2)
        
        if jq_process.returncode != 0:
            return None
            
        # Now use jq to extract specific fields we care about
        fields_process = subprocess.Popen(
            ["jq", "-r", "{ type: .type, message_type: (.message.content[0].type // null), tool_name: (.message.content[0].name // null), text_preview: (.message.content[0].text // null), subtype: .subtype, tools_count: (.tools | length), has_both: (.message.content | length > 1) }"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        fields_output, _ = fields_process.communicate(input=json_line.strip(), timeout=2)
        
        if fields_process.returncode == 0:
            # Parse the extracted fields
            fields = json.loads(fields_output)
            msg_type = fields.get('type')
            
            if msg_type == 'assistant':
                message_type = fields.get('message_type')
                has_both = fields.get('has_both', False)
                
                if message_type == 'text':
                    text = fields.get('text_preview', '')
                    # Smart truncation - try to end at word boundary
                    if len(text) > 120:
                        truncated = text[:120]
                        last_space = truncated.rfind(' ')
                        if last_space > 100:  # Only use word boundary if it's not too short
                            text = truncated[:last_space] + "..."
                        else:
                            text = truncated + "..."
                    return f"🤖 {text}"
                elif message_type == 'tool_use':
                    tool_name = fields.get('tool_name', 'unknown')
                    # Only show tool use if there's no accompanying text
                    if not has_both:
                        return f"🔧 {tool_name}"
                    else:
                        return None  # Skip, will be shown with the text
                else:
                    return "🤖 Assistant message"
            
            elif msg_type == 'user':
                # Don't show every user response, they're mostly tool results
                return None
            
            elif msg_type == 'system':
                subtype = fields.get('subtype', 'unknown')
                tools_count = fields.get('tools_count', 0) or 0
                return f"⚙️  System {subtype} | {tools_count} tools"
            
            else:
                return None  # Skip unknown types
                
    except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception):
        # Fallback to Python parsing if jq fails
        try:
            data = json.loads(json_line.strip())
            msg_type = data.get('type', 'unknown')
            
            if msg_type == 'assistant':
                content = data.get('message', {}).get('content', [])
                if content and len(content) > 0:
                    first_content = content[0]
                    if first_content.get('type') == 'text':
                        text = first_content.get('text', '')
                        if len(text) > 120:
                            truncated = text[:120]
                            last_space = truncated.rfind(' ')
                            if last_space > 100:
                                text = truncated[:last_space] + "..."
                            else:
                                text = truncated + "..."
                        return f"🤖 {text}"
                    elif first_content.get('type') == 'tool_use':
                        tool_name = first_content.get('name', 'unknown')
                        if len(content) == 1:  # Only tool use, no text
                            return f"🔧 {tool_name}"
                        else:
                            return None  # Skip if there's also text
                return None
            
            elif msg_type == 'user':
                return None  # Skip user responses
            
            elif msg_type == 'system':
                subtype = data.get('subtype', 'unknown')
                tools_count = len(data.get('tools', []))
                return f"⚙️  System {subtype} | {tools_count} tools"
            
            else:
                return None
                
        except:
            return None
    
    return None


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
    
    # Check if jq is available
    jq_available = False
    try:
        subprocess.run(["jq", "--version"], capture_output=True, check=True)
        jq_available = True
        print("🎨 Using jq for reliable JSON parsing", file=sys.stderr, flush=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("⚠️  jq not found, using Python JSON parsing fallback", file=sys.stderr, flush=True)

    while True:
        cmd = claude_cmd(continue_next)
        # Don't show the full prompt, just show that we're running claude
        print(f"Running: /usr/local/bin/claude {'--continue ' if continue_next else ''}[with prompt]", file=sys.stderr, flush=True)

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        # Stream output: save raw JSON to file, show clean summaries to user
        OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with OUTPUT_PATH.open("a", buffering=1) as log_fp:
            for line in process.stdout:  # type: ignore[attr-defined]
                # Always write raw JSON to log file for rate limit parsing
                log_fp.write(line)
                
                # Extract and display human-readable summary
                if line.strip():
                    timestamp = datetime.now().strftime("%H:%M:%S")
                    summary = extract_summary_with_jq(line) if jq_available else extract_summary_with_jq(line)
                    
                    if summary:
                        print(f"[{timestamp}] {summary}", flush=True)

        process.wait()

        continue_next = False  # Reset; will be re-enabled if rate-limited

        last_line = last_json_line(OUTPUT_PATH)
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

        # Always pause briefly to avoid spamming requests - and incase above rate limit parsing fails later
        time.sleep(60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Interrupted by user", file=sys.stderr)
