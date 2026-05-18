"""CLI client for the local agent API endpoint.

Examples:
  python agent_client.py --story ILREP-869 --action preview
  python agent_client.py --story ILREP-869 --action excel --include-testcases
  python agent_client.py --story ILREP-869 --action jira --created-by copilot-agent
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import requests


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Call Jira_Test_Generator agent API endpoint.")
    parser.add_argument("--url", default=os.environ.get("AGENT_API_URL", "http://127.0.0.1:5000"),
                        help="Base URL for Flask app (default: %(default)s)")
    parser.add_argument("--story", required=True, help="Jira story key, e.g. ILREP-869")
    parser.add_argument("--action", default="preview", choices=["preview", "excel", "jira"],
                        help="Execution mode")
    parser.add_argument("--jira-pat", default=os.environ.get("JIRA_PAT", ""),
                        help="Jira PAT token (or set JIRA_PAT env var)")
    parser.add_argument("--agent-token", default=os.environ.get("AGENT_API_TOKEN", ""),
                        help="Server agent API token (or set AGENT_API_TOKEN env var)")
    parser.add_argument("--custom-prompt", default="", help="Optional override prompt")
    parser.add_argument("--created-by", default="copilot-agent", help="Created_By value stored in DB")
    parser.add_argument("--include-testcases", action="store_true",
                        help="Include full testcase array in response")
    parser.add_argument("--timeout", type=int, default=180, help="HTTP timeout in seconds")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if not args.jira_pat:
        print("ERROR: Jira PAT is required. Use --jira-pat or set JIRA_PAT.", file=sys.stderr)
        return 2
    if not args.agent_token:
        print("ERROR: Agent API token is required. Use --agent-token or set AGENT_API_TOKEN.", file=sys.stderr)
        return 2

    endpoint = f"{args.url.rstrip('/')}/api/agent/generate"
    payload = {
        "story_key": args.story,
        "jira_pat": args.jira_pat,
        "action": args.action,
        "custom_prompt": args.custom_prompt,
        "created_by": args.created_by,
        "include_testcases": args.include_testcases,
    }
    headers = {
        "X-Agent-Token": args.agent_token,
        "Content-Type": "application/json",
    }

    try:
        resp = requests.post(endpoint, headers=headers, json=payload, timeout=args.timeout)
    except requests.RequestException as e:
        print(f"ERROR: Request failed: {e}", file=sys.stderr)
        return 1

    try:
        body = resp.json()
    except ValueError:
        print(f"ERROR: Non-JSON response ({resp.status_code}): {resp.text}", file=sys.stderr)
        return 1

    print(json.dumps(body, indent=2, ensure_ascii=False))
    return 0 if resp.ok and body.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
