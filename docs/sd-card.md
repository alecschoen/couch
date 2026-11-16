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

The Pi is ready for `install.sh`; see [Install on the Pi](../README.md#install-on-the-pi).

## Power cuts

The Pi has no power button that anyone uses: it gets unplugged, and the power goes out. It
has to shrug that off and boot straight back into the launcher with everything signed in.
`install.sh` sets this up (#25); `sudo ./install.sh --revert-sd` undoes all of it and leaves
the rest of Couch alone.

### What can go wrong

A cut can hurt in three ways:

1. **A file is half-written.** A program that rewrites a file in place leaves it empty or cut
   short when the power goes mid-write. This is the usual damage, and it's the program's
   doing, not the file system's.
2. **The file system is inconsistent.** ext4's journal replays at the next boot and fixes
   this, but only after fsck has run. The FAT boot partition has no journal.
3. **The card itself breaks.** A cheap card's controller can lose a block it was erasing,
   or a whole card wears out. The fewer writes, the smaller both risks.

### What Couch does

| | How | Where |
|---|---|---|
| Logs in RAM | journald `Storage=volatile`, at most 32 MB | `/etc/systemd/journald.conf.d/50-couch.conf` |
| `/tmp` in RAM | Trixie's own `tmp.mount`. Couch only turns it on for a system that lacks it | `/etc/systemd/system/local-fs.target.d/50-couch-tmp.conf`, if needed |
| No access-time writes | `noatime` on `/` (Pi OS has it already) and `/boot/firmware` | `/etc/fstab` through `couch-fstab`, original in `/etc/fstab.couch-orig` |
| Caches in RAM | a 256 MB tmpfs on `/var/cache/couch`. Chromium's disk cache (`--disk-cache-dir`, 100 MB at most) and Kodi's `special://temp` (`KODI_TEMP`, where `kodi.log` is) go there | `var-cache-couch.mount`, `/etc/tmpfiles.d/couch-cache.conf` |
| Repair, don't stop | `fsck.repair=yes` on the kernel command line, so a damaged file system is fixed at boot rather than dropping to a rescue shell nobody can reach. Pi OS has it already; Couch makes sure it stays | `cmdline.txt` through `couch-bootfiles` (owner `sd-card`) |
| Whole files only | couchd writes `settings.json` and `state.json` to a temporary file, fsyncs it, renames it over the old one and fsyncs the directory. A cut leaves the old file or the new one, and the temporary file left behind is cleared at the next start. `couchd.kodi_profile` writes Kodi's profile files the same way | `couchd/files.py` |
| No "Restore pages?" | after a cut Chromium thinks it crashed; `--hide-crash-restore-bubble` keeps the bubble off the TV | `system/couch-browser` |

What stays on the card, on purpose: the Chromium profile (`/var/lib/couch/chromium`: the
YouTube sign-in, cookies, local storage), the Kodi profile (`/var/lib/couch/kodi`: the Plex
sign-in, PM4K's settings, Kodi's thumbnails), couchd's settings and state, BlueZ's pairings
and the network config. These change rarely, and each program writes them in a way that
survives a crash. Chromium uses SQLite and LevelDB, which journal their writes, and replaces
its JSON files atomically. Kodi's databases are SQLite. BlueZ replaces its files atomically.
The old disk cache in the Chromium profile (`Default/Cache`) stays where it was. Nothing
writes to it any more, and Couch doesn't touch the profile.

What's left:
- Kodi rewrites `guisettings.xml` in place when it quits. A cut in that moment could leave
  it empty. ext4's `auto_da_alloc` writes a file that's truncated and rewritten before the
  next commit, which covers this pattern, and `sudo ./install.sh` writes Couch's Kodi
  settings again if they are ever lost.
- The FAT boot partition is only written by `install.sh` and kernel updates (#24, at
  night). A cut then can damage it; fsck.vfat repairs it at the next boot.
- The logs of the boot before a cut are gone: `journalctl -b -1` shows nothing after a
  reboot. To keep them while chasing a bug, `sudo ./install.sh --revert-sd`.

### Why not a read-only root (overlayfs)

Raspberry Pi OS can mount the root file system read-only with a RAM overlay on top
(`raspi-config` → Performance → Overlay File System). Nothing is ever written to the card,
so a cut can't damage anything there. That's the strongest protection there is, but it
doesn't fit a TV that's signed in to things and updates itself:

- **Everything written is lost at every reboot.** That includes the YouTube and Plex
  sign-ins as they refresh (Google rotates its session cookies, so a frozen profile drifts
  out of date and eventually gets signed out), the settings someone changes, speakers they
  pair, and Wi-Fi.
- **Updates (#24) need the overlay off.** Every overnight update would turn the overlay
  off, reboot, update, turn it on and reboot again. A cut during that runs with a writable
  root anyway, which is exactly the case this is meant to avoid.
- **Keeping the data means a writable partition after all.** Sign-ins and settings would
  need their own writable partition. That partition holds the same files that are at risk
  now (the profiles), so they'd be no safer, and repartitioning a card in place is a risk
  of its own.
- **The overlay fills RAM** with everything written since boot.

The files a cut can damage are the ones that must stay writable. So Couch makes those
writes safe, and moves everything else (logs, caches, `/tmp`, access times) to RAM or stops
it. The root file system stays writable.

If the card ever proves to be the weak spot, a better card helps more than an overlay
would: an "endurance" or industrial microSD (for example SanDisk High Endurance or
Max Endurance) is built for constant writes and power loss.

### Checking it

```bash
ssh couch.local
journalctl --header | grep -m1 'File path'            # /run/log/journal/...: in RAM
findmnt /tmp /var/cache/couch                          # both tmpfs
findmnt -n -o OPTIONS / /boot/firmware                 # both have noatime
/opt/couch/bin/couch-fstab show                        # what Couch added to fstab
/opt/couch/bin/couch-bootfiles show                    # ... and to cmdline.txt
cat /sys/block/mmcblk0/stat | awk '{print $7 * 512 / 1e6 " MB written since boot"}'
```

If the Pi doesn't boot after a cut, put the card in a PC. `etc/fstab.couch-orig` on the
root partition and `cmdline.txt.couch-orig` on the boot partition are the files as they were
before Couch; copy them over the originals.
