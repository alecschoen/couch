#!/usr/bin/env bash
# Install or update Couch on Raspberry Pi OS Lite (64-bit, Trixie). Safe to re-run: every
# step compares before it writes, so a second run changes nothing and restarts nothing.
#
#   sudo ./install.sh              # install or update from this checkout
#   sudo ./install.sh --uninstall  # remove Couch, keep /etc/couch and /var/lib/couch
#   sudo ./install.sh --purge      # remove Couch with its config, state and user
#
# Without a checkout next to it (curl ... | sudo bash) it clones the repo into
# /opt/couch/src first; set COUCH_REPO to clone from somewhere else.
#
# It writes only to /opt/couch, /etc/couch, /var/lib/couch, the two units in
# /etc/systemd/system, the couch user, and apt packages.
set -euo pipefail
trap 'echo "error: install.sh stopped at line $LINENO" >&2' ERR

PREFIX=/opt/couch
ETC=/etc/couch
STATE=/var/lib/couch
UNIT_DIR=/etc/systemd/system
UNITS=(couchd.service couch-session.service)
COUCH_USER=couch
COUCH_GROUPS=(video render input audio bluetooth)
REPO=${COUCH_REPO:-https://github.com/alecschoen/couch.git}
PACKAGES=(
    cage chromium                        # the session: Wayland kiosk + browser
    kodi kodi-inputstream-adaptive       # Plex via Kodi (#22)
    pipewire wireplumber pipewire-pulse  # audio
    bluez
    python3-venv python3-aiohttp python3-dbus-fast python3-hatchling python3-editables
    fonts-noto-core
    rsync
)

changes=0
USER_CHANGED=0
say() { printf '\033[1m%s\033[0m\n' "$*"; }
note() { changes=$((changes + 1)); echo "  $*"; }
die() { echo "error: $*" >&2; exit 1; }

# --- helpers: each one does nothing when things already match ---------------------------

ensure_packages() {
    local p missing=()
    for p in "$@"; do
        dpkg-query -W -f='${db:Status-Status}' "$p" 2>/dev/null | grep -qx installed || missing+=("$p")
    done
    ((${#missing[@]})) || return 0
    apt-get update -q
    DEBIAN_FRONTEND=noninteractive apt-get install -y -q "${missing[@]}"
    note "installed ${missing[*]}"
}

# ensure_dir PATH OWNER MODE
ensure_dir() {
    local path=$1 owner=$2 mode=$3
    if [[ -d $path && $(stat -c '%U:%G %a' "$path") == "$owner:$owner $mode" ]]; then return 0; fi
    install -d -o "$owner" -g "$owner" -m "$mode" "$path"
    note "set up $path"
}

# ensure_file SRC DST MODE; returns 0 if it wrote DST, 1 if DST was already the same.
ensure_file() {
    local src=$1 dst=$2 mode=$3
    if cmp -s "$src" "$dst" && [[ $(stat -c %a "$dst") == "$mode" ]]; then return 1; fi
    install -D -m "$mode" "$src" "$dst"
    note "wrote $dst"
}

# ensure_tree SRC_DIR DST_DIR; returns 0 if anything under DST changed.
ensure_tree() {
    local out
    out=$(rsync -rlp --checksum --delete --exclude=__pycache__ --chmod=D755,F644 \
        --itemize-changes "$1/" "$2/")
    [[ -n $out ]] || return 1
    note "updated $2 ($(wc -l <<<"$out") files)"
}

# --- install ------------------------------------------------------------------------------

find_source() {
    local here=""
    if [[ -f ${BASH_SOURCE[0]:-} ]]; then
        here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
    fi
    if [[ -n $here && -f $here/pyproject.toml && -d $here/couchd ]]; then
        SRC=$here
        return
    fi
    # Piped from curl: keep a checkout of our own.
    ensure_packages git
    SRC=$PREFIX/src
    if [[ -d $SRC/.git ]]; then
        git -C "$SRC" fetch -q --depth 1 origin
        if [[ $(git -C "$SRC" rev-parse HEAD) != $(git -C "$SRC" rev-parse FETCH_HEAD) ]]; then
            git -C "$SRC" reset -q --hard FETCH_HEAD
            note "updated $SRC to $(git -C "$SRC" rev-parse --short HEAD)"
        fi
    else
        git clone -q --depth 1 "$REPO" "$SRC"
        note "cloned $REPO"
    fi
}

check_system() {
    local codename arch
    codename=$(. /etc/os-release && echo "${VERSION_CODENAME:-}")
    arch=$(dpkg --print-architecture)
    [[ $codename == trixie && $arch == arm64 ]] \
        || echo "warning: built for Pi OS Lite 64-bit (trixie, arm64); this is $codename, $arch" >&2
}

ensure_user() {
    if ! id -u "$COUCH_USER" >/dev/null 2>&1; then
        useradd --system --user-group --home-dir "$STATE" --no-create-home \
            --shell /usr/sbin/nologin --comment "Couch TV session" "$COUCH_USER"
        note "created user $COUCH_USER"
    fi
    local g
    for g in "${COUCH_GROUPS[@]}"; do
        if ! id -nG "$COUCH_USER" | tr ' ' '\n' | grep -qx "$g"; then
            usermod -aG "$g" "$COUCH_USER"
            note "added $COUCH_USER to $g"
            USER_CHANGED=1 # running services only see new groups after a restart
        fi
    done
}

install_couch() {
    local code=$USER_CHANGED session=$USER_CHANGED units=0

    ensure_dir "$PREFIX" root 755
    ensure_dir "$ETC" root 755
    ensure_dir "$STATE" "$COUCH_USER" 755
    ensure_dir "$STATE/chromium" "$COUCH_USER" 700

    # couchd runs from an editable install, so its bundled UI is /opt/couch/ui.
    ensure_tree "$SRC/couchd" "$PREFIX/couchd" && code=1
    ensure_tree "$SRC/ui" "$PREFIX/ui" && session=1
    local pyproject=0
    ensure_file "$SRC/pyproject.toml" "$PREFIX/pyproject.toml" 644 && pyproject=1
    ensure_file "$SRC/README.md" "$PREFIX/README.md" 644 || true
    if [[ ! -x $PREFIX/.venv/bin/python ]]; then
        python3 -m venv --system-site-packages "$PREFIX/.venv"
        note "created $PREFIX/.venv"
        pyproject=1
    fi
    if ((pyproject)) || [[ ! -x $PREFIX/.venv/bin/couchd ]]; then
        "$PREFIX/.venv/bin/pip" install -q --no-deps --no-build-isolation --no-index -e "$PREFIX"
        note "installed couchd into $PREFIX/.venv"
        code=1
    fi

    ensure_file "$SRC/system/couch-browser" "$PREFIX/bin/couch-browser" 755 && session=1
    # The config is yours: written once, never overwritten.
    if [[ ! -e $ETC/config.toml ]]; then
        install -m 644 "$SRC/system/config.example.toml" "$ETC/config.toml"
        note "wrote $ETC/config.toml"
    fi

    # Plex via Kodi (#22) adds its Kodi profile and the script.plexmod addon here.

    local u
    for u in "${UNITS[@]}"; do
        if ensure_file "$SRC/system/$u" "$UNIT_DIR/$u" 644; then
            units=1
            [[ $u == couchd.service ]] && code=1 || session=1
        fi
    done
    ((units)) && systemctl daemon-reload
    for u in "${UNITS[@]}"; do
        if ! systemctl -q is-enabled "$u" 2>/dev/null; then
            systemctl -q enable "$u"
            note "enabled $u"
        fi
    done

    # Restart only what changed; start what isn't running.
    if ((code)) || ! systemctl -q is-active couchd.service; then
        systemctl restart couchd.service
        note "restarted couchd"
    fi
    if ((session)) || ! systemctl -q is-active couch-session.service; then
        systemctl restart couch-session.service
        note "restarted the TV session"
    fi
}

# --- uninstall ----------------------------------------------------------------------------

uninstall() {
    local purge=$1 u
    for u in "${UNITS[@]}"; do
        systemctl disable --now "$u" 2>/dev/null || true
        rm -f "$UNIT_DIR/$u"
    done
    systemctl daemon-reload
    rm -rf "$PREFIX"
    if ((purge)); then
        rm -rf "$ETC" "$STATE"
        if id -u "$COUCH_USER" >/dev/null 2>&1; then userdel "$COUCH_USER"; fi
        echo "Removed Couch with its config, state and user."
    else
        echo "Removed Couch. Kept $ETC and $STATE; --purge removes them and the user."
    fi
    # Give tty1 its login prompt back.
    systemctl start getty@tty1.service 2>/dev/null || true
    echo "Apt packages stay installed: ${PACKAGES[*]}"
}

# --- main ---------------------------------------------------------------------------------

[[ $EUID == 0 ]] || die "run as root: sudo $0 $*"

case ${1:-} in
--uninstall) uninstall 0; exit 0 ;;
--purge) uninstall 1; exit 0 ;;
"") ;;
*) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac

check_system
say "Packages"
ensure_packages "${PACKAGES[@]}"
say "Source"
find_source
echo "  from $SRC"
say "User"
ensure_user
say "Couch"
install_couch

echo
if ((changes)); then
    say "Done: $changes changes. The launcher is on the TV."
else
    say "Nothing to change; Couch is up to date."
fi
