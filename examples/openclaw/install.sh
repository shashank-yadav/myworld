#!/usr/bin/env bash
set -euo pipefail

openclaw mcp add myworld-gmail \
  --command uvx \
  --arg myworld \
  --arg stdio \
  --arg gmail \
  --include 'search_emails,read_email,send_email'

openclaw mcp doctor myworld-gmail --probe

