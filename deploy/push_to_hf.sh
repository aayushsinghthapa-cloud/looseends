#!/usr/bin/env bash
# Publish the current commit to a Hugging Face Space (Docker SDK).
#
# One-time setup:
#   1. Create a Space at https://huggingface.co/new-space : name "looseends", SDK "Docker", template "Blank".
#   2. Create a write token at https://huggingface.co/settings/tokens
#   3. Optional: in the Space's Settings > Variables and secrets, add a secret GROQ_API_KEY.
#
# Usage:
#   HF_TOKEN=hf_xxx ./deploy/push_to_hf.sh <hf-username> [space-name]
set -euo pipefail

USER_NAME="${1:?usage: HF_TOKEN=... $0 <hf-username> [space-name]}"
SPACE="${2:-looseends}"
: "${HF_TOKEN:?set HF_TOKEN to a Hugging Face write token}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

git clone --depth 1 "https://user:${HF_TOKEN}@huggingface.co/spaces/${USER_NAME}/${SPACE}" "$WORK/space"
# Replace the Space contents with the current commit, plus the Space's own README header.
find "$WORK/space" -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
git -C "$ROOT" archive HEAD | tar -x -C "$WORK/space"
cp "$ROOT/deploy/huggingface/README.md" "$WORK/space/README.md"
rm -rf "$WORK/space/docs/media"  # large video and images stay on GitHub

cd "$WORK/space"
git add -A
git -c user.name="$USER_NAME" -c user.email="${USER_NAME}@users.noreply.huggingface.co" \
  commit -q -m "Deploy $(git -C "$ROOT" rev-parse --short HEAD)" || { echo "Nothing to deploy."; exit 0; }
git push -q origin HEAD:main
echo "Pushed. Build logs: https://huggingface.co/spaces/${USER_NAME}/${SPACE}?logs=build"
echo "App: https://${USER_NAME}-${SPACE}.hf.space"
