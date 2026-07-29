#!/bin/bash
# Commit everything and push, riding over the 15-minute cron's commits.
#
# Usage:
#   ./push.sh "commit message"     (message optional, defaults to "Update")
#
# If the push is rejected because the GitHub Action advanced main, this
# rebases and retries. Conflicts can only happen on the cron's data files
# (albums.json / heatmap_data.json) — we keep the local version, since the
# cron re-corrects the data within 15 minutes of the push anyway.
cd "$(dirname "$0")"
MSG="${1:-Update}"

git add -A
if git diff --cached --quiet; then
    echo "Nothing new to commit — pushing any unpushed commits."
else
    git commit -m "$MSG" || exit 1
fi

for attempt in 1 2 3; do
    if git push; then
        echo "Pushed."
        exit 0
    fi
    echo "Push rejected (cron moved main) — rebasing (attempt $attempt)..."
    if ! git pull --rebase; then
        git checkout --theirs albums.json heatmap_data.json 2>/dev/null
        git add albums.json heatmap_data.json 2>/dev/null
        if ! GIT_EDITOR=true git rebase --continue; then
            echo "Rebase needs manual attention: run 'git status'"
            exit 1
        fi
    fi
done

echo "Still failing after 3 attempts — check 'git status' and the remote."
exit 1
