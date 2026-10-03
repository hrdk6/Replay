#!/usr/bin/env bash
# Trigger platform deploys for an image SHA. Works with Render deploy hooks (which accept
# ?imgURL= for image-backed services); any webhook that takes an image URL works the same way.
# Missing hooks are skipped with a notice so the workflow is useful before hosting exists.
set -euo pipefail

owner=$(echo "${OWNER:?}" | tr '[:upper:]' '[:lower:]')
sha="${SHA:?}"

deploy() {
  local name="$1" hook="$2" image="$3"
  if [ -z "$hook" ]; then
    echo "::notice::no deploy hook for $name; skipping"
    return 0
  fi
  local encoded
  encoded=$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$image")
  # The hook URL is a secret: never echo it.
  curl --fail --silent --show-error -X POST "${hook}&imgURL=${encoded}" > /dev/null
  echo "triggered $name -> $image"
}

deploy api "${API_HOOK:-}" "ghcr.io/$owner/replay-api:$sha"
deploy worker "${WORKER_HOOK:-}" "ghcr.io/$owner/replay-api:$sha"
deploy web "${WEB_HOOK:-}" "ghcr.io/$owner/replay-web:$sha"
