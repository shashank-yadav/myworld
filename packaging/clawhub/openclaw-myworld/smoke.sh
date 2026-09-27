#!/usr/bin/env bash
set -euo pipefail

clawhub package validate ./packaging/clawhub/openclaw-myworld
clawhub package publish ./packaging/clawhub/openclaw-myworld --family code-plugin --dry-run
