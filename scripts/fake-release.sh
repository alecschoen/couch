#!/usr/bin/env bash
# Build a fake Couch release for trying couch-update (#24) without a GitHub release or tag:
# a tarball of this checkout (`git archive`, so committed files only) and a GitHub-shaped
# latest.json pointing at it.
#
#   scripts/fake-release.sh DIR TAG BASE_URL [--broken]
#   scripts/fake-release.sh ~/couch-fake v0.0.2 http://127.0.0.1:8799
#   python3 -m http.server 8799 --bind 127.0.0.1 -d ~/couch-fake
#   # /etc/couch/update.toml: source = "http://127.0.0.1:8799/latest.json"
#
# --broken makes couchd exit at once, so the health check fails and couch-update must roll
# back. BASE_URL can be file:///path/to/DIR too.
set -euo pipefail
[[ $# -ge 3 ]] || { sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }
dir=$1 tag=$2 base=${3%/} broken=${4:-}
cd "$(dirname "$0")/.."
mkdir -p "$dir"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
top=alecschoen-couch-$(git rev-parse --short HEAD)
git archive --format=tar --prefix="$top/" HEAD | tar -x -C "$work"
if [[ $broken == --broken ]]; then
    # couchd dies before it says READY, so systemd restarts it until it gives up. Only the
    # daemon: install.sh itself (which runs couchd.kodi_profile) still works.
    printf '\nraise SystemExit("fake broken release (scripts/fake-release.sh --broken)")\n' \
        >>"$work/$top/couchd/daemon.py"
fi
tar -czf "$dir/couch-$tag.tar.gz" -C "$work" "$top"
cat >"$dir/latest.json" <<JSON
{
  "tag_name": "$tag",
  "name": "Couch $tag (fake)",
  "draft": false,
  "prerelease": false,
  "tarball_url": "$base/couch-$tag.tar.gz",
  "assets": []
}
JSON
echo "$dir/couch-$tag.tar.gz${broken:+ (broken)}, $dir/latest.json"
