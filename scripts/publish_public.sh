#!/usr/bin/env bash
# Sync the tracked tree of HEAD to the public GitHub mirror.
#
# The mirror carries no history from this repo (it started as a squashed
# "Initial public release"), so every sync is a single snapshot commit.
# Files listed in PRIVATE_PATHS never leave this repo.
#
# Usage: scripts/publish_public.sh ["commit message"]
set -euo pipefail

REMOTE="${PUBLIC_REPO_REMOTE:-git@github.com:airbone42/kleinanzeigen-bot.git}"
BRANCH="${PUBLIC_REPO_BRANCH:-main}"
PRIVATE_PATHS=("CLAUDE.md")

MESSAGE="${1:-chore: sync from private repo}"

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "error: working tree has uncommitted changes; commit them first." >&2
    exit 1
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

git clone --quiet --depth 1 --branch "$BRANCH" "$REMOTE" "$WORK/mirror"

# Replace the mirror's content with the current tracked tree.
find "$WORK/mirror" -mindepth 1 -maxdepth 1 -not -name .git -exec rm -rf {} +
git archive HEAD | tar -x -C "$WORK/mirror"
for path in "${PRIVATE_PATHS[@]}"; do
    rm -rf "${WORK:?}/mirror/${path}"
done

cd "$WORK/mirror"
git add -A
if git diff --cached --quiet; then
    echo "mirror already up to date."
    exit 0
fi
git commit --quiet -m "$MESSAGE"
git push --quiet origin "$BRANCH"
echo "pushed $(git rev-parse --short HEAD) to $REMOTE ($BRANCH)"
