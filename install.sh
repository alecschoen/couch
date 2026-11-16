#!/usr/bin/env bash
# Install or update Couch on Raspberry Pi OS Lite (64-bit, Trixie). Safe to re-run: every
# step compares before it writes, so a second run changes nothing and restarts nothing.
#
#   sudo ./install.sh              # install or update from this checkout
#   sudo ./install.sh --uninstall  # remove Couch, keep /etc/couch and /var/lib/couch
#   sudo ./install.sh --purge      # remove Couch with its config, state and user
#   sudo ./install.sh --revert-boot  # only put the boot files back (Couch stays)
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
# (/etc/cloud/cloud-init.disabled).
set -euo pipefail
trap 'echo "error: install.sh stopped at line $LINENO" >&2' ERR

PREFIX=/opt/couch
ETC=/etc/couch
STATE=/var/lib/couch
UNIT_DIR=/etc/systemd/system
UNITS=(couchd.service couch-session.service)
# Installed but not enabled: couchd starts Kodi when Plex is launched.
STATIC_UNITS=(couch-kodi.service)
POLKIT_RULE=/etc/polkit-1/rules.d/50-couch.rules
UINPUT_RULE=/etc/udev/rules.d/60-couch-uinput.rules
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
CLOUD_INIT_MARK="# Couch's install.sh turned cloud-init off after the first boot; --uninstall removes this"
PACKAGES=(
    cage chromium                        # the session: Wayland kiosk + browser
    kodi kodi-inputstream-adaptive       # Plex via Kodi (#22)
    pipewire wireplumber pipewire-pulse  # audio (#10)
    pulseaudio-utils                     # pactl: how couchd talks to PipeWire
    bluez libspa-0.2-bluetooth           # Bluetooth, and PipeWire's plugin for its audio
    polkitd                              # lets couchd switch the screen to Kodi and back
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
# unblock it; systemd-rfkill keeps that across reboots. Wi-Fi is left alone.
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
    local code=$USER_CHANGED session=$USER_CHANGED units=0

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
    ensure_file "$SRC/system/couch-kodi" "$PREFIX/bin/couch-kodi" 755 || true
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
    if [[ -d /etc/cloud && ! -e $CLOUD_INIT_OFF ]] \
        && cloud-init status 2>/dev/null | grep -qx 'status: done'; then
        echo "$CLOUD_INIT_MARK" >"$CLOUD_INIT_OFF"
        note "turned cloud-init off after its first boot ($CLOUD_INIT_OFF)"
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
    if [[ -e $INITRAMFS_HOOK || -e $SPLASH_TGA ]]; then
        rm -f "$INITRAMFS_HOOK" "$SPLASH_TGA"
        update-initramfs -u -k all >/dev/null
        echo "Took the splash out of the initramfs."
    fi
    if [[ -e $FBSPLASH_RULE || -e $FBSPLASH_UNIT ]]; then
        rm -f "$FBSPLASH_RULE" "$FBSPLASH_UNIT" "$PREFIX/bin/couch-fbsplash"
        udevadm control --reload 2>/dev/null || true
        systemctl daemon-reload
    fi
    # Only if Couch turned cloud-init off; someone else's cloud-init.disabled stays.
    if [[ -f $CLOUD_INIT_OFF ]] && [[ $(<"$CLOUD_INIT_OFF") == "$CLOUD_INIT_MARK" ]]; then
        rm -f "$CLOUD_INIT_OFF"
        echo "Turned cloud-init back on."
    fi
}

uninstall() {
    local purge=$1 u
    uninstall_boot
    systemctl stop "${STATIC_UNITS[@]}" 2>/dev/null || true
    for u in "${UNITS[@]}"; do
        systemctl disable --now "$u" 2>/dev/null || true
        rm -f "$UNIT_DIR/$u"
    done
    for u in "${STATIC_UNITS[@]}"; do rm -f "$UNIT_DIR/$u"; done
    rm -f "$POLKIT_RULE" "$UINPUT_RULE" "$UINPUT_MODULE_CONF"
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
"") ;;
*) sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
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
say "Couch"
install_couch
install_bluetooth_audio
say "Boot"
install_boot

echo
if ((changes)); then
    say "Done: $changes changes. The launcher is on the TV."
    if ((REBOOT)); then echo "The new boot screen shows from the next boot: sudo reboot"; fi
else
    say "Nothing to change; Couch is up to date."
fi
