#!/usr/bin/env bash
# Flash Raspberry Pi OS Lite (64-bit) onto an SD card, ready for Couch: hostname, your user
# with your SSH key (no password), SSH on, your timezone, keyboard and locale. Ethernet uses
# DHCP out of the box. See docs/sd-card.md.
#
#   scripts/flash-sd.sh --check /dev/sdX     # show what would happen, change nothing
#   scripts/flash-sd.sh /dev/sdX             # erase the card and write it
#   scripts/flash-sd.sh --print-user-data    # just print the cloud-init config
#
# Overrides: COUCH_HOSTNAME, COUCH_USER, COUCH_SSH_KEY (path to a .pub), COUCH_TIMEZONE,
# COUCH_KEYBOARD, COUCH_LOCALE.
set -euo pipefail
trap 'echo "error: $0 stopped at line $LINENO" >&2' ERR

IMAGE_DATE=2026-10-06
IMAGE=${IMAGE_DATE}-raspios-trixie-arm64-lite.img.xz
URL=https://downloads.raspberrypi.com/raspios_lite_arm64/images/raspios_lite_arm64-${IMAGE_DATE}/${IMAGE}
SHA256=483db18a48da399b5b7022ffae9a07bc6d99daab30e593aedac09b69ff422843
CACHE=${XDG_CACHE_HOME:-$HOME/.cache}/couch
MAX_BYTES=$((256 * 1024 * 1024 * 1024)) # nothing bigger than this is an SD card

die() { echo "error: $*" >&2; exit 1; }
say() { printf '\033[1m%s\033[0m\n' "$*"; }

# --- settings -------------------------------------------------------------------------------

pick_key() {
    if [[ -n ${COUCH_SSH_KEY:-} ]]; then echo "$COUCH_SSH_KEY"; return; fi
    for k in id_ed25519 id_ecdsa id_rsa; do
        [[ -f $HOME/.ssh/$k.pub ]] && { echo "$HOME/.ssh/$k.pub"; return; }
    done
    die "no SSH public key in ~/.ssh; make one with ssh-keygen -t ed25519, or set COUCH_SSH_KEY"
}

PI_HOSTNAME=${COUCH_HOSTNAME:-couch}
PI_USER=${COUCH_USER:-$USER}
KEY_FILE=$(pick_key)
TIMEZONE=${COUCH_TIMEZONE:-$(timedatectl show -p Timezone --value 2>/dev/null || true)}
TIMEZONE=${TIMEZONE:-UTC}
LAYOUT=${COUCH_KEYBOARD:-$(localectl status 2>/dev/null | awk -F': *' '/X11 Layout/ {print $2}')}
LAYOUT=${LAYOUT:-us}
LOCALE=${COUCH_LOCALE:-${LANG:-en_GB.UTF-8}}

[[ $PI_HOSTNAME =~ ^[a-z0-9-]+$ ]] || die "hostname must be lowercase letters, digits and -"
[[ $PI_USER =~ ^[a-z_][a-z0-9_-]*$ ]] || die "bad user name: $PI_USER"
[[ -f $KEY_FILE ]] || die "no such key file: $KEY_FILE"
KEY=$(head -n1 "$KEY_FILE")
[[ $KEY =~ ^(ssh-|ecdsa-|sk-) ]] || die "$KEY_FILE doesn't look like a public key"

user_data() {
    cat <<EOF
#cloud-config
# Written by couch/scripts/flash-sd.sh. Applied once, on first boot.
hostname: ${PI_HOSTNAME}
manage_etc_hosts: true
timezone: ${TIMEZONE}
locale: ${LOCALE}
keyboard:
  model: pc105
  layout: ${LAYOUT}

# One admin user, SSH key only. It has no password, so console login is off until you
# set one with: sudo passwd ${PI_USER}
users:
  - name: ${PI_USER}
    gecos: Couch admin
    groups: [users, adm, dialout, audio, netdev, video, plugdev, input, render, sudo]
    shell: /bin/bash
    lock_passwd: true
    sudo: "ALL=(ALL) NOPASSWD:ALL"
    ssh_authorized_keys:
      - ${KEY}
ssh_pwauth: false
EOF
}

# --- the card -------------------------------------------------------------------------------

check_device() {
    local dev=$1
    [[ -b $dev ]] || die "$dev is not a block device"
    [[ $(lsblk -dno TYPE "$dev") == disk ]] || die "$dev is not a whole disk (give /dev/sdX, not a partition)"

    local tran removable size
    tran=$(lsblk -dno TRAN "$dev" | tr -d ' ')
    removable=$(lsblk -dno RM "$dev" | tr -d ' ')
    size=$(lsblk -dbno SIZE "$dev" | tr -d ' ')
    [[ $tran == usb || $tran == mmc || $removable == 1 ]] \
        || die "$dev is not a USB or SD device (transport: ${tran:-none}); refusing"
    (( size > 0 )) || die "$dev has no card in it"
    (( size <= MAX_BYTES )) || die "$dev is $((size / 1024**3)) GiB; too big to be an SD card, refusing"
    if lsblk -no FSTYPE "$dev" | grep -q linux_raid_member; then
        die "$dev is part of a RAID array; refusing"
    fi
    if lsblk -no MOUNTPOINTS "$dev" | grep -qE '^(/|/boot.*|/home.*|/var.*|/usr.*|\[SWAP\])$'; then
        die "$dev holds a system mount; refusing"
    fi
}

ensure_image() {
    mkdir -p "$CACHE"
    local file=$CACHE/$IMAGE
    if [[ ! -f $file ]]; then
        say "Downloading $IMAGE"
        curl -fL -C - --retry 3 -o "$file.part" "$URL"
        mv "$file.part" "$file"
    fi
    echo "$SHA256  $file" | sha256sum -c --quiet - || die "checksum mismatch for $file; delete it and retry"
    echo "$file"
}

unmount_all() {
    local dev=$1 part
    for part in $(lsblk -lnpo NAME "$dev" | tail -n +2); do
        if [[ -n $(lsblk -no MOUNTPOINTS "$part") ]]; then
            udisksctl unmount -b "$part" >/dev/null 2>&1 || sudo umount "$part"
        fi
    done
}

# Unpack the image and put the first-boot files into its boot partition here on the PC,
# so the card only ever gets one sequential write. Nothing reads the card back: cheap
# readers can write fine yet lock up on reads.
prepare_image() {
    local xz=$1 img=$CACHE/${IMAGE%.xz} start fat tmp
    say "Unpacking the image and adding first-boot settings" >&2
    xz -dc "$xz" > "$img.part"
    mv "$img.part" "$img"
    start=$(partx -g -n 1 -o START "$img" | tr -d ' ')
    [[ $start =~ ^[0-9]+$ ]] || die "can't find the boot partition in $img"
    fat="$img@@$((start * 512))"
    export MTOOLS_SKIP_CHECK=1
    mtype -i "$fat" ::cmdline.txt >/dev/null || die "$img has no Pi OS boot partition"

    tmp=$(mktemp -d)
    user_data > "$tmp/user-data"
    : > "$tmp/ssh" # sshswitch.service turns SSH on at first boot
    mcopy -o -i "$fat" "$tmp/user-data" "$tmp/ssh" ::
    cmp -s <(mtype -i "$fat" ::user-data) "$tmp/user-data" || die "user-data didn't land in $img"
    rm -r "$tmp"
    echo "$img"
}

summary() {
    cat <<EOF
  hostname:  ${PI_HOSTNAME}  (reach it as ${PI_HOSTNAME}.local)
  user:      ${PI_USER}, SSH key $(basename "$KEY_FILE"), no password
  timezone:  ${TIMEZONE}
  keyboard:  ${LAYOUT}
  locale:    ${LOCALE}
  network:   Ethernet, DHCP
  image:     ${IMAGE}
EOF
}

# --- main -----------------------------------------------------------------------------------

case ${1:-} in
--print-user-data)
    user_data
    exit 0
    ;;
--check)
    [[ -n ${2:-} ]] || die "usage: $0 --check /dev/sdX"
    check_device "$2"
    lsblk -o NAME,SIZE,TRAN,MODEL,LABEL,FSTYPE,MOUNTPOINTS "$2"
    echo
    say "$2 looks like an SD card. Flashing it would write:"
    summary
    exit 0
    ;;
/dev/*)
    DEV=$1
    ;;
*)
    sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac

check_device "$DEV"
command -v mcopy >/dev/null || die "needs mtools to edit the image: sudo pacman -S mtools (or apt install mtools)"
FILE=$(ensure_image)

echo
lsblk -o NAME,SIZE,TRAN,MODEL,LABEL,FSTYPE,MOUNTPOINTS "$DEV"
echo
say "Everything on $DEV will be erased. It will get:"
summary
echo
read -r -p "Type the device name ($(basename "$DEV")) to continue: " answer
[[ $answer == "$(basename "$DEV")" ]] || die "not confirmed; nothing was written"

IMG=$(prepare_image "$FILE")
unmount_all "$DEV"
say "Writing the image (a few minutes)"
sudo dd if="$IMG" of="$DEV" bs=4M oflag=direct conv=fsync status=progress
rm -f "$IMG"
udisksctl power-off -b "$DEV" >/dev/null 2>&1 || true

echo
say "Done. Put the card in the Pi, plug in Ethernet, HDMI (port 0, next to USB-C) and power."
echo "First boot takes a minute or two. Then: ssh ${PI_USER}@${PI_HOSTNAME}.local"
