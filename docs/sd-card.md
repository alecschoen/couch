# SD card: a fresh base for Couch

Couch starts from a clean **Raspberry Pi OS Lite (64-bit)**, Trixie. No desktop, no Kodi
setup from before. This guide gets you from a blank (or old Kodi) card to a Pi you can SSH
into, ready for `install.sh`.

You need a microSD card (16 GB or more), a card reader, and Ethernet to the Pi. Wi-Fi works
too but is slower to set up.

## 0. Back up the old card (optional)

If the card has your Kodi setup and you might want it back, image it first. Find the card
with `lsblk` (here `/dev/sdX`):

```bash
sudo dd if=/dev/sdX bs=4M status=progress | zstd -T0 > ~/kodi-card-backup.img.zst
```

To restore it later: `zstdcat ~/kodi-card-backup.img.zst | sudo dd of=/dev/sdX bs=4M conv=fsync status=progress`.

## 1. Flash the card

### On Linux: `scripts/flash-sd.sh`

The script downloads the pinned Pi OS image, checks its SHA-256, and adds the first-boot
settings to a copy of the image on your PC. Then it writes that copy to the card in one pass.
It never reads the card back, because cheap USB readers often write fine but lock up on reads.
It needs `mtools` (`sudo pacman -S mtools`, or `sudo apt install mtools`).

```bash
lsblk                                     # find the card, e.g. /dev/sdX
scripts/flash-sd.sh --check /dev/sdX      # dry run: shows the card and the settings
scripts/flash-sd.sh /dev/sdX              # erase and write; asks you to type the name
```

It takes these settings from your PC: your user name, `~/.ssh/id_ed25519.pub`, timezone,
keyboard layout and locale. The hostname is `couch`. Override any of them with
`COUCH_USER`, `COUCH_SSH_KEY`, `COUCH_TIMEZONE`, `COUCH_KEYBOARD`, `COUCH_LOCALE` or
`COUCH_HOSTNAME`.

It refuses anything that isn't a USB or SD device, anything bigger than 256 GB, and any disk
that's part of a RAID array or holds a system mount. Only the `dd` step uses `sudo`.

**What it writes to the boot partition:**
- `user-data`: cloud-init config with the hostname, timezone, locale, keyboard, and one
  admin user. The user logs in with your SSH key only and has passwordless sudo.
- An empty `ssh` file, so `sshswitch.service` turns SSH on at first boot.

`network-config` stays as shipped: Ethernet uses DHCP. The admin user has no password, so
logging in at the Pi's own console is off until you set one (`sudo passwd`).

### Anywhere: Raspberry Pi Imager

1. **Choose device** → Raspberry Pi 4. **Choose OS** → Raspberry Pi OS (other) →
   **Raspberry Pi OS Lite (64-bit)**.
2. Choose the card, then **Edit settings**:
   - Hostname: `couch`
   - Username: your choice, with a password
   - Wi-Fi: only if there's no Ethernet
   - Locale: your timezone and keyboard layout
   - **Services**: enable SSH, *Allow public-key authentication only*, and paste your
     public key (`cat ~/.ssh/id_ed25519.pub`)
3. Write.

## 2. First boot

Plug the card into the Pi, then connect:
- Ethernet
- HDMI into the TV, using **HDMI 0**, the micro-HDMI port next to the USB-C power port.
  That's the port CEC uses.
- Power, last

The first boot resizes the card and runs cloud-init. Give it about two minutes, then:

```bash
ssh couch.local
sudo apt update && sudo apt full-upgrade -y
sudo reboot
```

If SSH says `Permission denied (publickey)` and your key has a passphrase, load the key into
your agent first (`ssh-add ~/.ssh/id_ed25519`).

On first boot cloud-init reports `degraded done` because of a missing `cc_netplan_nm_patch`
module. That comes from the stock Pi OS config and is harmless.

If `couch.local` doesn't resolve, find the Pi's address in your router's client list and use
`ssh <user>@<ip>`.

If SSH warns that the host key changed (you've had a Pi called `couch` before), clear
the old entry: `ssh-keygen -R couch.local`.

## 3. Next

The Pi is ready for `install.sh` (#3).
