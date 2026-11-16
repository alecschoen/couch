#!/usr/bin/env bash
# Install or update Couch on Raspberry Pi OS Lite (64-bit, Trixie). Safe to re-run: every
# step compares before it writes, so a second run changes nothing and restarts nothing.
#
#   sudo ./install.sh              # install or update from this checkout
#   sudo ./install.sh --uninstall  # remove Couch, keep /etc/couch and /var/lib/couch
#   sudo ./install.sh --purge      # remove Couch with its config, state and user
#   sudo ./install.sh --revert-boot  # only put the boot files back (Couch stays)
#   sudo ./install.sh --revert-sd    # only undo the SD card protection (Couch stays)
#
# Without a checkout next to it (curl ... | sudo bash) it clones the repo into
# /opt/couch/src first; set COUCH_REPO to clone from somewhere else.
#
# It writes only to /opt/couch, /etc/couch, /var/lib/couch, the three units in
# /etc/systemd/system, one polkit rule, one WirePlumber drop-in, one udev rule and one
# modules-load.d file (for /dev/uinput), the couch user, and apt packages. It also turns on
# PipeWire's user units for every user (they come enabled; this makes sure). Plex's Kodi
# addons are downloaded at the versions and sha256s pinned in system/kodi/addons.toml. For the
# silent boot it edits cmdline.txt and config.txt through couch-bootfiles, which keeps the
# originals and undoes its edits on --uninstall. It also puts the splash in the initramfs
# (/lib/firmware/couch-splash.tga and one initramfs hook), adds couch-fbsplash with its unit
# and udev rule, and turns cloud-init off once the first boot is done
# (/etc/cloud/cloud-init.disabled). Against power cuts (docs/sd-card.md) it keeps the journal
# in RAM (one journald.conf.d file), puts Chromium's and Kodi's caches on a tmpfs (one mount
# unit, one tmpfiles.d file), and adds noatime to /etc/fstab through couch-fstab, which keeps
# the original and undoes its edits on --uninstall.
# Overnight updates (#24) add couch-update's units and timer, one apt.conf.d file,
# /var/lib/couch-update, and a copy of this source in /opt/couch/source to roll back to.
# The timer and unattended-upgrades only with `nightly = true` in /etc/couch/update.toml.
# For Wi-Fi (#31) it adds a polkit rule, a drop-in for systemd-rfkill, and couch-wifi-boot
# with its unit and udev rule; it never switches Wi-Fi itself.
set -euo pipefail
trap 'echo "error: install.sh stopped at line $LINENO" >&2' ERR

PREFIX=/opt/couch
ETC=/etc/couch
STATE=/var/lib/couch
UNIT_DIR=/etc/systemd/system
UNITS=(couchd.service couch-session.service)
# Installed but not enabled: couchd starts Kodi when Plex is launched.
STATIC_UNITS=(couch-kodi.service)
KODI_VT_GETTYS=(getty@tty2.service autovt@tty2.service)
POLKIT_RULE=/etc/polkit-1/rules.d/50-couch.rules
UINPUT_RULE=/etc/udev/rules.d/60-couch-uinput.rules
NO_POINTER_RULE=/etc/udev/rules.d/70-couch-no-pointer.rules
UINPUT_MODULE_CONF=/etc/modules-load.d/couch-uinput.conf
WIREPLUMBER_CONF=/etc/wireplumber/wireplumber.conf.d/50-couch.conf
# Bluetooth's is in couch's own WirePlumber config, not /etc: anyone else's WirePlumber (an
# ssh login) must stay off Bluetooth, or the two fight over the speaker.
WIREPLUMBER_BT_DIR=$STATE/.config/wireplumber/wireplumber.conf.d
WIREPLUMBER_BT_CONF=$WIREPLUMBER_BT_DIR/51-couch-bluetooth.conf
AUDIO_USER_UNITS=(pipewire.socket pipewire-pulse.socket wireplumber.service)
COUCH_USER=couch
COUCH_GROUPS=(video render input audio bluetooth)
REPO=${COUCH_REPO:-https://github.com/alecschoen/couch.git}
# Overnight updates (#24): couch-update (couchd/updater.py) runs as root from the timer, or
# from couchd's Check now through couch-update-now.service.
UPDATE_UNITS=(couch-update.service couch-update-now.service couch-update.timer)
UPDATE_TIMER=couch-update.timer
UPDATE_STATE=/var/lib/couch-update
APT_UPDATES_CONF=/etc/apt/apt.conf.d/52couch-updates
# What install.sh needs from a checkout; copied to /opt/couch/source for a rollback.
SOURCE_FILES=(install.sh pyproject.toml README.md couchd ui system)
# Silent boot (#4). couch-bootfiles makes these edits to cmdline.txt and config.txt as the
# owner "boot", and `revert boot` undoes exactly them. Other boot settings get an owner of
# their own (see the top of system/couch-bootfiles).
BOOTFILES=$PREFIX/bin/couch-bootfiles
BOOT_CMDLINE=(
    quiet loglevel=0                  # no kernel or systemd text
    vt.global_cursor_default=0        # no cursor
    consoleblank=0
    console=tty3                      # the console leaves the TV's tty1 (Ctrl+Alt+F3 has it)
    fullscreen_logo=1 fullscreen_logo_name=couch-splash.tga # the kernel draws the splash
)
# Not logo.nologo: it would hide the fullscreen logo too, which replaces the penguins anyway.
BOOT_CMDLINE_DROP=(console=tty1)
SPLASH_TGA=/lib/firmware/couch-splash.tga
INITRAMFS_HOOK=/etc/initramfs-tools/hooks/couch-splash
FBSPLASH_UNIT=$UNIT_DIR/couch-fbsplash.service
FBSPLASH_RULE=/etc/udev/rules.d/70-couch-fbsplash.rules
CLOUD_INIT_OFF=/etc/cloud/cloud-init.disabled
WIFI_BOOT_UNIT=$UNIT_DIR/couch-wifi-boot.service
WIFI_RULE=/etc/udev/rules.d/70-couch-wifi.rules
RFKILL_DROPIN=$UNIT_DIR/systemd-rfkill.service.d/couch.conf
CLOUD_INIT_MARK="# Couch's install.sh turned cloud-init off after the first boot; --uninstall removes this"
# Power cuts (#25, docs/sd-card.md). couch-fstab and couch-bootfiles make these edits as
# the owner "sd-card"; `install.sh --revert-sd` undoes exactly them, and the files below.
FSTAB_TOOL=$PREFIX/bin/couch-fstab
SD_NOATIME=(/ /boot/firmware)          # mount points that get noatime, if fstab has them
SD_CMDLINE=(fsck.repair=yes)           # repair the file systems after a cut; no rescue shell
SD_JOURNALD_CONF=/etc/systemd/journald.conf.d/50-couch.conf
SD_CACHE=/var/cache/couch
SD_CACHE_MOUNT=$UNIT_DIR/var-cache-couch.mount
SD_CACHE_TMPFILES=/etc/tmpfiles.d/couch-cache.conf
SD_TMP_CONF=$UNIT_DIR/local-fs.target.d/50-couch-tmp.conf
CACHE_CHANGED=0
PACKAGES=(
    cage chromium                        # the session: Wayland kiosk + browser
    kodi kodi-inputstream-adaptive       # Plex via Kodi (#22)
    pipewire wireplumber pipewire-pulse  # audio (#10)
    pulseaudio-utils                     # pactl: how couchd talks to PipeWire
    bluez libspa-0.2-bluetooth           # Bluetooth, and PipeWire's plugin for its audio
    polkitd                              # lets couchd switch to Kodi and back, and reboot
    python3-venv python3-aiohttp python3-dbus-fast python3-hatchling python3-editables
    python3-evdev                        # the remote's virtual keyboard (#8)
    fonts-noto-core
    rsync
)

changes=0
USER_CHANGED=0
REBOOT=0
say() { printf '\033[1m%s\033[0m\n' "$*"; }
note() { changes=$((changes + 1)); echo "  $*"; }
die() { echo "error: $*" >&2; exit 1; }

# --- helpers: each one does nothing when things already match ---------------------------

installed() { dpkg-query -W -f='${db:Status-Status}' "$1" 2>/dev/null | grep -qx installed; }

ensure_packages() {
    local p missing=()
    for p in "$@"; do
        installed "$p" || missing+=("$p")
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

# Raspberry Pi OS starts with the radios soft-blocked (rfkill). Couch needs Bluetooth, so
# unblock it; systemd-rfkill keeps that across reboots. Wi-Fi is left alone: the
# network.wifi setting drives it (install_wifi).
# Returns 0 if it unblocked anything.
unblock_bluetooth() {
    local r changed=1
    for r in /sys/class/rfkill/rfkill*; do
        [[ -r $r/type && $(<"$r/type") == bluetooth && $(<"$r/soft") == 1 ]] || continue
        echo 0 >"$r/soft"
        changed=0
    done
    ((changed)) || note "unblocked Bluetooth (rfkill)"
    return $changed
}

# Wi-Fi (#31). couchd switches it with NetworkManager from the network.wifi setting (off
# unless turned on in Settings > Network); the polkit rule lets it. This only makes "off"
# survive a reboot without a failed unit: systemd-rfkill leaves Wi-Fi alone (it hangs on
# the Pi's chip at boot) and couch-wifi-boot blocks the radio once wlan0 is up, if
# NetworkManager has it off. Nothing here switches Wi-Fi now. See system/README.md, "Wi-Fi".
install_wifi() {
    local reload=0
    ensure_file "$SRC/system/couch-wifi-boot" "$PREFIX/bin/couch-wifi-boot" 755 || true
    ensure_file "$SRC/system/systemd-rfkill-couch.conf" "$RFKILL_DROPIN" 644 && reload=1
    ensure_file "$SRC/system/couch-wifi-boot.service" "$WIFI_BOOT_UNIT" 644 && reload=1
    ((reload)) && systemctl daemon-reload
    if ensure_file "$SRC/system/70-couch-wifi.rules" "$WIFI_RULE" 644; then
        udevadm control --reload-rules
    fi
    return 0
}

# WirePlumber reads its config and looks for PipeWire's Bluetooth plugin only when it
# starts. Without the plugin BlueZ has nothing to send a speaker's sound to and drops the
# connection; the config is in system/wireplumber-bluetooth.conf.
install_bluetooth_audio() {
    local restart=0
    ((HAD_BT_AUDIO)) || restart=1
    ensure_dir "$STATE/.config" "$COUCH_USER" 700
    ensure_dir "$STATE/.config/wireplumber" "$COUCH_USER" 700
    ensure_dir "$WIREPLUMBER_BT_DIR" "$COUCH_USER" 700
    ensure_file "$SRC/system/wireplumber-bluetooth.conf" "$WIREPLUMBER_BT_CONF" 644 && restart=1
    ((restart)) || return 0
    systemctl --user -M "$COUCH_USER@" -q is-active wireplumber.service 2>/dev/null || return 0
    systemctl --user -M "$COUCH_USER@" restart wireplumber.service
    note "restarted WirePlumber so it picks up Bluetooth audio"
}

# The remote's keys reach Chromium and Kodi through a virtual keyboard on /dev/uinput (#8),
# which couchd (user couch, group input) must be able to open. The udev rule covers every
# boot; this boot's node is fixed up directly. Returns 0 if couchd must restart to open it.
install_uinput() {
    local changed=1
    ensure_file "$SRC/system/couch-uinput.conf" "$UINPUT_MODULE_CONF" 644 || true
    if ensure_file "$SRC/system/60-couch-uinput.rules" "$UINPUT_RULE" 644; then
        udevadm control --reload-rules
    fi
    # And no mouse cursor: the CEC inputs aren't pointers (system/70-couch-no-pointer.rules).
    # libinput reads it when a device is added, so it takes effect from the next boot.
    if ensure_file "$SRC/system/70-couch-no-pointer.rules" "$NO_POINTER_RULE" 644; then
        udevadm control --reload-rules
        REBOOT=1
    fi
    if [[ ! -e /dev/uinput ]]; then
        modprobe uinput
        note "loaded uinput"
        changed=0
    fi
    if [[ $(stat -c '%G %a' /dev/uinput) != "input 660" ]]; then
        chgrp input /dev/uinput
        chmod 660 /dev/uinput
        note "let group input use /dev/uinput"
        changed=0
    fi
    return $changed
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
    local code=$USER_CHANGED session=$((USER_CHANGED || CACHE_CHANGED)) units=0

    ensure_dir "$PREFIX" root 755
    ensure_dir "$ETC" root 755
    ensure_dir "$STATE" "$COUCH_USER" 755
    ensure_dir "$STATE/chromium" "$COUCH_USER" 700
    ensure_dir "$STATE/kodi" "$COUCH_USER" 700

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
    # cage's client: couch-splash over everything until the launcher has drawn, then
    # Chromium (#23). couch-splash draws with couch-fbsplash's code, installed below.
    ensure_file "$SRC/system/couch-session-start" "$PREFIX/bin/couch-session-start" 755 && session=1
    ensure_file "$SRC/system/couch-splash" "$PREFIX/bin/couch-splash" 755 && session=1
    ensure_file "$SRC/system/couch-kodi" "$PREFIX/bin/couch-kodi" 755 || true
    ensure_file "$SRC/system/couch-kodi-vt" "$PREFIX/bin/couch-kodi-vt" 755 || true
    # polkitd picks up rule changes by itself.
    ensure_file "$SRC/system/couch.rules" "$POLKIT_RULE" 644 || true
    # The config is yours: written once, never overwritten.
    if [[ ! -e $ETC/config.toml ]]; then
        install -m 644 "$SRC/system/config.example.toml" "$ETC/config.toml"
        note "wrote $ETC/config.toml"
    fi

    install_kodi_profile

    install_audio
    install_uinput && code=1
    install_wifi

    local u
    for u in "${UNITS[@]}"; do
        if ensure_file "$SRC/system/$u" "$UNIT_DIR/$u" 644; then
            units=1
            [[ $u == couchd.service ]] && code=1 || session=1
        fi
    done
    for u in "${STATIC_UNITS[@]}"; do
        if ensure_file "$SRC/system/$u" "$UNIT_DIR/$u" 644; then units=1; fi
    done
    ((units)) && systemctl daemon-reload
    # tty2 is Kodi's (couch-kodi.service). logind starts a login prompt on a console as soon
    # as it's switched to, and one on tty2 fights Kodi for the tty and leaves the screen in a
    # state cage can't take back. So none there. Masked by hand, under both names logind and
    # systemd use: `systemctl mask autovt@tty2` resolves the alias and masks getty@tty2 only.
    local masked=0
    for u in "${KODI_VT_GETTYS[@]}"; do
        if [[ $(readlink "$UNIT_DIR/$u") != /dev/null ]]; then
            ln -sfn /dev/null "$UNIT_DIR/$u"
            masked=1
            note "masked $u"
        fi
    done
    if ((masked)); then
        systemctl daemon-reload
        systemctl -q stop "${KODI_VT_GETTYS[@]}" 2>/dev/null || true
    fi
    for u in "${UNITS[@]}"; do
        if ! systemctl -q is-enabled "$u" 2>/dev/null; then
            systemctl -q enable "$u"
            note "enabled $u"
        fi
    done

    # couchd powers the radio when it starts, so restart it if the radio was blocked.
    unblock_bluetooth && code=1

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

# Plex via Kodi (#22): PM4K and its dependencies at pinned versions, Couch's service addon
# and skin, the splash, and Kodi's settings. couchd/kodi_profile.py does the work, as couch,
# who owns the profile. Kodi rewrites its settings when it exits, so if Plex is on screen and
# the profile needs changing, Kodi is stopped first (the launcher comes back).
install_kodi_profile() {
    local profile=(runuser -u "$COUCH_USER" -- "$PREFIX/.venv/bin/python" -m couchd.kodi_profile
        --home "$STATE/kodi" --src "$PREFIX/kodi")
    ensure_tree "$SRC/system/kodi" "$PREFIX/kodi" || true
    if systemctl -q is-active couch-kodi.service && [[ -n $("${profile[@]}" --dry-run) ]]; then
        systemctl stop couch-kodi.service
        note "stopped Plex to update Kodi's profile"
    fi
    local out line
    out=$("${profile[@]}")
    while IFS= read -r line; do
        [[ -z $line ]] || note "kodi: $line"
    done <<<"$out"
}

# Sound (#10). PipeWire runs in the couch user's session, which cage's logind session on
# tty1 starts. couchd, a system service running as couch, reaches it at
# /run/user/<uid>/pulse/native, so nothing else needs opening up.
install_audio() {
    local u
    for u in "${AUDIO_USER_UNITS[@]}"; do
        if ! systemctl --global -q is-enabled "$u" 2>/dev/null; then
            systemctl --global -q enable "$u"
            note "enabled $u for user sessions"
        fi
    done
    if ensure_file "$SRC/system/wireplumber-couch.conf" "$WIREPLUMBER_CONF" 644; then
        # Picked up at WirePlumber's next start; restart it now if couch's session is up.
        systemctl --user -M "$COUCH_USER@" -q restart wireplumber.service 2>/dev/null || true
    fi
}

# Overnight updates (#24). couch-update installs a release by running its install.sh, and
# goes back by running the previous one's, which is this copy. /opt/couch/VERSION says which
# Couch this is (source_version).
# Which Couch this is: a checkout says so through git (`v1.2.0`, `v1.2.0-3-gabc1234`, or
# `dev-abc1234` before the first tag; `-dirty` with local changes), a release through the
# VERSION file couch-update writes into it. Neither: nothing, and VERSION goes, so Settings
# never shows a version this isn't (a leftover from a release installed before).
source_version() {
    local src=$1 v
    if [[ -e $src/.git ]]; then
        command -v git >/dev/null || return 0
        v=$(git -c safe.directory="$src" -C "$src" describe --tags --always --dirty \
            --match 'v[0-9]*' 2>/dev/null) || return 0
        [[ $v == v[0-9]* ]] || v=dev-$v
        echo "$v"
    elif [[ -f $src/VERSION ]]; then
        head -n1 "$src/VERSION"
    fi
}

install_updates() {
    local src_dir=$PREFIX/source version="" f units=0
    if [[ $(realpath "$SRC") != $(realpath -m "$src_dir") ]]; then
        local out
        out=$(cd "$SRC" && rsync -rlp --checksum --delete --exclude=__pycache__ \
            --chmod=D755,F644 --itemize-changes --relative "${SOURCE_FILES[@]}" "$src_dir/")
        [[ -z $out ]] || note "kept a copy of this version in $src_dir"
    fi
    version=$(source_version "$SRC")
    for f in "$PREFIX/VERSION" "$src_dir/VERSION"; do
        if [[ -z $version ]]; then
            rm -f "$f"
        elif [[ ! -f $f || $(<"$f") != "$version" ]]; then
            echo "$version" >"$f"
            [[ $f == "$PREFIX/VERSION" ]] && note "this is Couch $version"
        fi
    done
    ensure_file "$SRC/system/apt-couch.conf" "$APT_UPDATES_CONF" 644 || true
    ensure_file "$SRC/system/update.example.toml" "$ETC/update.example.toml" 644 || true
    for f in "${UPDATE_UNITS[@]}"; do
        ensure_file "$SRC/system/$f" "$UNIT_DIR/$f" 644 && units=1
    done
    ((units)) && systemctl daemon-reload
    # The night runs are opt-in (`nightly = true` in update.toml): until then no timer and
    # no unattended-upgrades. apt-couch.conf keeps apt's own daily run off either way.
    local nightly=0 code=0
    /usr/bin/python3 -I "$SRC/couchd/updater.py" enabled --config "$ETC/update.toml" \
        || code=$?
    case $code in
        0) nightly=1 ;;
        1) ;;
        *) echo "  warning: $ETC/update.toml is broken; overnight updates stay off" >&2 ;;
    esac
    if ((nightly)); then
        ensure_packages unattended-upgrades
        if ! systemctl -q is-enabled "$UPDATE_TIMER" 2>/dev/null; then
            systemctl -q enable --now "$UPDATE_TIMER"
            note "enabled overnight updates ($UPDATE_TIMER)"
        fi
    elif systemctl -q is-enabled "$UPDATE_TIMER" 2>/dev/null; then
        systemctl -q disable --now "$UPDATE_TIMER"
        note "overnight updates are off ($UPDATE_TIMER disabled)"
    fi
}

uninstall_updates() {
    local u
    systemctl disable --now "$UPDATE_TIMER" 2>/dev/null || true
    for u in "${UPDATE_UNITS[@]}"; do rm -f "$UNIT_DIR/$u"; done
    rm -f "$APT_UPDATES_CONF" "$ETC/update.example.toml"
    rm -rf "$UPDATE_STATE"
}

# The silent boot (#4). The firmware shows nothing (no rainbow square). The kernel draws the
# Couch splash as soon as there's a framebuffer and prints nothing on the TV. The splash stays
# until cage takes the screen for the launcher. All of this takes effect at the next boot.
install_boot() {
    ensure_file "$SRC/system/couch-bootfiles" "$BOOTFILES" 755 || true
    if command -v update-initramfs >/dev/null; then
        # The splash has to be in the initramfs: the firmware's framebuffer, which the kernel
        # draws it on, comes up before the root filesystem does.
        local initramfs=0
        ensure_file "$SRC/ui/brand/splash.tga" "$SPLASH_TGA" 644 && initramfs=1
        ensure_file "$SRC/system/initramfs-splash" "$INITRAMFS_HOOK" 755 && initramfs=1
        if ((initramfs)); then
            update-initramfs -u -k all >/dev/null
            note "rebuilt the initramfs with the splash"
            REBOOT=1
        fi
    fi
    # The kernel draws it only until vc4 takes the screen over; couch-fbsplash draws it on
    # vc4's framebuffer from then until cage.
    ensure_file "$SRC/system/couch-fbsplash" "$PREFIX/bin/couch-fbsplash" 755 || true
    if ensure_file "$SRC/system/couch-fbsplash.service" "$FBSPLASH_UNIT" 644; then
        systemctl daemon-reload
        REBOOT=1
    fi
    if ensure_file "$SRC/system/couch-fbsplash.rules" "$FBSPLASH_RULE" 644; then
        udevadm control --reload 2>/dev/null || true
        REBOOT=1
    fi
    local args=() t out line
    for t in "${BOOT_CMDLINE[@]}"; do args+=(--set "$t"); done
    for t in "${BOOT_CMDLINE_DROP[@]}"; do args+=(--drop "$t"); done
    out=$("$BOOTFILES" apply boot --config "$SRC/system/boot-config.txt" "${args[@]}")
    if [[ -n $out ]]; then
        while IFS= read -r line; do note "$line"; done <<<"$out"
        REBOOT=1
    fi
    # cloud-init only matters on a card's first boot, where it sets up the user and SSH key
    # (docs/sd-card.md). After that it adds about 2.5 s to every boot, before anything else
    # starts. Pi OS's network doesn't come from it: NetworkManager configures eth0 itself.
    # `cloud-init status` exits 2 for "degraded done" (Pi OS's first boot ends that way), so
    # read its first line rather than its exit status.
    local status=
    if [[ -d /etc/cloud && ! -e $CLOUD_INIT_OFF ]]; then
        status=$(cloud-init status 2>/dev/null) || true
    fi
    if [[ ${status%%$'\n'*} == "status: done" ]]; then
        echo "$CLOUD_INIT_MARK" >"$CLOUD_INIT_OFF"
        note "turned cloud-init off after its first boot ($CLOUD_INIT_OFF)"
        REBOOT=1
    fi
}

# True if /tmp is on the card and systemd's tmp.mount could put it in RAM.
tmp_needs_tmpfs() {
    [[ $(findmnt -n -o FSTYPE /tmp 2>/dev/null) != tmpfs ]] || return 1
    ! findmnt --fstab -n /tmp >/dev/null || return 1 # fstab has a say over /tmp; leave it
    systemctl cat tmp.mount >/dev/null 2>&1 || return 1
    [[ $(systemctl is-enabled tmp.mount 2>/dev/null) != masked ]]
}

# Power cuts (#25). The SD card should shrug off losing power at any moment, so Couch keeps
# what's written all the time off it, and what's left on it is written so a cut leaves the
# old or the new version (couchd/files.py). docs/sd-card.md has the reasoning, and why the
# root file system stays writable (no overlayfs). Runs before install_couch: the session
# must start with its cache directories in place.
install_sd_card() {
    ensure_file "$SRC/system/couch-fstab" "$FSTAB_TOOL" 755 || true
    ensure_file "$SRC/system/couch-bootfiles" "$BOOTFILES" 755 || true

    # The journal in RAM. A restart applies it now; services' log streams survive it.
    if ensure_file "$SRC/system/journald-couch.conf" "$SD_JOURNALD_CONF" 644; then
        systemctl restart systemd-journald.service
        note "the journal is in RAM from now on"
    fi

    # Chromium's and Kodi's caches on a tmpfs, with their directories made at every boot.
    local reload=0
    ensure_file "$SRC/system/var-cache-couch.mount" "$SD_CACHE_MOUNT" 644 && reload=1
    ensure_file "$SRC/system/couch-cache.conf" "$SD_CACHE_TMPFILES" 644 || true
    # /tmp in RAM: Trixie does that already (tmp.mount); this is for a system that doesn't.
    if tmp_needs_tmpfs; then
        ensure_file "$SRC/system/couch-tmp.conf" "$SD_TMP_CONF" 644 && reload=1 && REBOOT=1
    fi
    ((reload)) && systemctl daemon-reload
    if ! systemctl -q is-enabled var-cache-couch.mount 2>/dev/null; then
        systemctl -q enable var-cache-couch.mount
        note "enabled var-cache-couch.mount"
    fi
    if ! mountpoint -q "$SD_CACHE"; then
        systemctl start var-cache-couch.mount
        note "mounted a tmpfs on $SD_CACHE"
        CACHE_CHANGED=1
    fi
    systemd-tmpfiles --create "$SD_CACHE_TMPFILES"

    # noatime: reading a file doesn't write to the card. Pi OS has it on / already.
    local mp targets=() out line
    for mp in "${SD_NOATIME[@]}"; do
        findmnt --fstab -n "$mp" >/dev/null && targets+=("$mp=noatime")
    done
    if ((${#targets[@]})); then
        out=$("$FSTAB_TOOL" apply sd-card "${targets[@]}")
        if [[ -n $out ]]; then
            while IFS= read -r line; do note "$line"; done <<<"$out"
            systemctl daemon-reload
        fi
        for mp in "${targets[@]%=noatime}"; do
            if [[ ,$(findmnt -n -o OPTIONS "$mp"), != *,noatime,* ]]; then
                mount -o remount,noatime "$mp"
                note "remounted $mp with noatime"
            fi
        done
    fi

    local args=() t
    for t in "${SD_CMDLINE[@]}"; do args+=(--set "$t"); done
    out=$("$BOOTFILES" apply sd-card "${args[@]}")
    if [[ -n $out ]]; then
        while IFS= read -r line; do note "$line"; done <<<"$out"
        REBOOT=1
    fi
}

# --- uninstall ----------------------------------------------------------------------------

# Undo the silent boot: couch-bootfiles puts back what it changed in cmdline.txt and
# config.txt, then the splash leaves the initramfs.
uninstall_boot() {
    # The copy next to this script first: it can undo what an older installed copy can't.
    local tool
    tool=$(dirname "${BASH_SOURCE[0]:-$0}")/system/couch-bootfiles
    [[ -x $tool ]] || tool=$BOOTFILES
    if [[ -x $tool ]]; then
        "$tool" revert boot
    elif [[ -e /boot/firmware/couch-boot.json ]]; then
        echo "warning: couch-bootfiles is missing, so cmdline.txt and config.txt keep Couch's" \
            "boot settings. The originals are the .couch-orig files next to them." >&2
    fi
    # The rule first: initramfs-tools copies /etc/udev/rules.d into the initramfs.
    if [[ -e $FBSPLASH_RULE || -e $FBSPLASH_UNIT ]]; then
        rm -f "$FBSPLASH_RULE" "$FBSPLASH_UNIT" "$PREFIX/bin/couch-fbsplash"
        udevadm control --reload 2>/dev/null || true
        systemctl daemon-reload
    fi
    if [[ -e $INITRAMFS_HOOK || -e $SPLASH_TGA ]]; then
        rm -f "$INITRAMFS_HOOK" "$SPLASH_TGA"
        update-initramfs -u -k all >/dev/null
        echo "Took the splash out of the initramfs."
    fi
    # Only if Couch turned cloud-init off; someone else's cloud-init.disabled stays.
    if [[ -f $CLOUD_INIT_OFF ]] && [[ $(<"$CLOUD_INIT_OFF") == "$CLOUD_INIT_MARK" ]]; then
        rm -f "$CLOUD_INIT_OFF"
        echo "Turned cloud-init back on."
    fi
}

# Undo install_sd_card. What's mounted now stays mounted until the next boot: Chromium may
# be using the cache, and remounting / without noatime gains nothing.
uninstall_sd_card() {
    local here fstab_tool bootfiles
    here=$(dirname "${BASH_SOURCE[0]:-$0}")/system
    fstab_tool=$here/couch-fstab
    [[ -x $fstab_tool ]] || fstab_tool=$FSTAB_TOOL
    bootfiles=$here/couch-bootfiles
    [[ -x $bootfiles ]] || bootfiles=$BOOTFILES
    if [[ -x $fstab_tool ]]; then
        "$fstab_tool" revert sd-card
    elif [[ -e /etc/fstab.couch.json ]]; then
        echo "warning: couch-fstab is missing, so /etc/fstab keeps Couch's noatime." \
            "The original is /etc/fstab.couch-orig." >&2
    fi
    [[ -x $bootfiles ]] && "$bootfiles" revert sd-card
    if [[ -e $SD_JOURNALD_CONF ]]; then
        rm -f "$SD_JOURNALD_CONF"
        systemctl restart systemd-journald.service
        echo "The journal is on the SD card again."
    fi
    if [[ -e $SD_CACHE_MOUNT || -e $SD_CACHE_TMPFILES || -e $SD_TMP_CONF ]]; then
        systemctl -q disable var-cache-couch.mount 2>/dev/null || true
        rm -f "$SD_CACHE_MOUNT" "$SD_CACHE_TMPFILES" "$SD_TMP_CONF"
        systemctl daemon-reload
        echo "Chromium's and Kodi's caches go back to their profiles from the next boot."
    fi
}

uninstall() {
    local purge=$1 u
    uninstall_sd_card
    uninstall_boot
    uninstall_updates
    systemctl stop "${STATIC_UNITS[@]}" 2>/dev/null || true
    for u in "${UNITS[@]}"; do
        systemctl disable --now "$u" 2>/dev/null || true
        rm -f "$UNIT_DIR/$u"
    done
    for u in "${STATIC_UNITS[@]}"; do rm -f "$UNIT_DIR/$u"; done
    for u in "${KODI_VT_GETTYS[@]}"; do
        if [[ $(readlink "$UNIT_DIR/$u") == /dev/null ]]; then rm -f "$UNIT_DIR/$u"; fi
    done
    rm -f "$POLKIT_RULE" "$UINPUT_RULE" "$NO_POINTER_RULE" "$UINPUT_MODULE_CONF"
    rm -f "$WIFI_RULE" "$WIFI_BOOT_UNIT" "$RFKILL_DROPIN"
    rmdir "$(dirname "$RFKILL_DROPIN")" 2>/dev/null || true
    systemctl daemon-reload
    rm -f "$WIREPLUMBER_CONF"
    rm -rf "$PREFIX"
    rm -f "$WIREPLUMBER_BT_CONF"
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
    echo "The boot settings are back as they were, from the next boot."
}

# --- main ---------------------------------------------------------------------------------

[[ $EUID == 0 ]] || die "run as root: sudo $0 $*"

case ${1:-} in
--uninstall) uninstall 0; exit 0 ;;
--purge) uninstall 1; exit 0 ;;
--revert-boot) uninstall_boot; echo "Boot files are back as they were, from the next boot."; exit 0 ;;
--revert-sd) uninstall_sd_card; echo "The SD card protection is off, fully from the next boot."; exit 0 ;;
"") ;;
*) sed -n '2,31p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac

check_system
say "Packages"
installed libspa-0.2-bluetooth && HAD_BT_AUDIO=1 || HAD_BT_AUDIO=0
ensure_packages "${PACKAGES[@]}"
say "Source"
find_source
echo "  from $SRC"
say "User"
ensure_user
say "SD card"
install_sd_card
say "Couch"
install_couch
install_bluetooth_audio
say "Boot"
install_boot
say "Updates"
install_updates

echo
if ((changes)); then
    say "Done: $changes changes. The launcher is on the TV."
    if ((REBOOT)); then echo "The new boot screen shows from the next boot: sudo reboot"; fi
else
    say "Nothing to change; Couch is up to date."
fi
