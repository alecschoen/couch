# System files

Everything `install.sh` puts outside `/opt/couch`: systemd units, the cage/Chromium session, the hidden Kodi profile, and `config.txt`/`cmdline.txt` changes for a silent boot.

- `couchd.service`: the background service (`Type=notify`, watchdog, restarts on failure)
- `couch-session.service`: cage on tty1 as the `couch` user, through a logind session. It's an autologin without a password, and it takes tty1 from the console login.
- `couch-browser`: the Chromium command line cage runs (kiosk, Wayland, profile in `/var/lib/couch/chromium`, DevTools on 127.0.0.1:9222 for couchd)
- `couch-kodi.service`: Kodi on tty1 for Plex. Not enabled: couchd starts it, and starting it stops `couch-session.service` (they conflict). When Kodi exits the unit starts the session again, so the launcher comes back even without couchd.
- `couch-kodi`: the Kodi command line (DRM/GBM, profile in `/var/lib/couch/kodi`, filled by #22)
- `couch.rules`: polkit rules letting the `couch` user start, stop and reset those two units, reboot or power off the Pi (Settings › Power), and switch Wi-Fi on and off (Settings › Network), nothing else. Installed as `/etc/polkit-1/rules.d/50-couch.rules`.
- `config.example.toml`: every config key with its default
- `couch-bootfiles`: edits `cmdline.txt` and `config.txt` in `/boot/firmware`, and undoes exactly those edits. Each change has an owner (`boot` for the silent boot), so other boot settings can come and go on their own. It keeps the first copy of each file as `*.couch-orig`. Installed as `/opt/couch/bin/couch-bootfiles`; run it with `show` to see what Couch changed.
- `boot-config.txt`: the silent boot's lines for `config.txt` (`disable_splash=1`, `disable_overscan=1`), in a `# >>> couch:boot` block at the end.
- `initramfs-splash`: an initramfs-tools hook, installed as `/etc/initramfs-tools/hooks/couch-splash`. It puts the splash (`/lib/firmware/couch-splash.tga`, from `ui/brand/splash.tga`) in the initramfs.
- `70-couch-no-pointer.rules`: no mouse cursor on the TV. The kernel's CEC remote inputs (`vc4-hdmi-0`, `vc4-hdmi-1`) have mouse axes, so libinput would hand them to cage as pointers and cage would draw its cursor. This makes libinput ignore them. couchd doesn't use them: it reads `/dev/cec0` and types through its own Couch remote device.
- `couch-fbsplash`, `couch-fbsplash.service`, `couch-fbsplash.rules`: draw the same splash again on vc4's framebuffer when vc4 takes the screen over, and once more at shutdown (see Boot below). The rule is installed as `/etc/udev/rules.d/70-couch-fbsplash.rules`.
- `couch-wifi-boot`, `couch-wifi-boot.service`, `70-couch-wifi.rules`, `systemd-rfkill-couch.conf`: keep Wi-Fi off across a reboot without a failed unit (see Wi-Fi below). The drop-in is installed as `/etc/systemd/system/systemd-rfkill.service.d/couch.conf`, the rule as `/etc/udev/rules.d/70-couch-wifi.rules`.
- `wireplumber-couch.conf`: installed as `/etc/wireplumber/wireplumber.conf.d/50-couch.conf`. couchd picks the output and moves every stream itself, so WirePlumber is told not to send apps back to the output they last used.

## Sound

PipeWire runs as the `couch` user, in the session `couch-session.service` opens on tty1 (logind starts `user@<uid>.service`, which starts PipeWire, WirePlumber and pipewire-pulse). couchd is a system service, but it runs as `couch` too, so it reaches that PipeWire at `/run/user/<uid>/pulse/native` with `pactl`. It needs no linger, extra groups or socket permissions. couchd starts before the session, so it waits for PipeWire, and it reconnects if PipeWire restarts.

To look at the same thing couchd sees, on the Pi:

```bash
sudo -u couch XDG_RUNTIME_DIR=/run/user/$(id -u couch) pactl list sinks short
sudo -u couch XDG_RUNTIME_DIR=/run/user/$(id -u couch) wpctl status
```

## Wi-Fi

Couch is meant to be on Ethernet, so Wi-Fi is an option in Settings › Network, off unless someone turns it on (the `network.wifi` setting, #31). couchd applies it when it starts and whenever it changes, through NetworkManager's `WirelessEnabled` over D-Bus (`couchd/networkmanager.py`). NetworkManager then soft-blocks or unblocks the radio (rfkill) and remembers the choice in `/var/lib/NetworkManager/NetworkManager.state`. couchd only switches the radio. It never joins a network: a network NetworkManager already knows reconnects by itself once the radio is on. `install.sh` never switches Wi-Fi either, so an existing install keeps what it has until couchd applies the setting. One exception: if the setting was never touched and Wi-Fi is the Pi's only way onto the network (no Ethernet), couchd leaves it on and saves that as the setting, so a Pi set up for Wi-Fi only stays reachable.

The boot needed a fix. At boot, systemd-rfkill puts back each radio's saved block. On the Pi 4 it reaches Wi-Fi about 6.4 s in, while `brcmfmac` is still starting the chip (wlan0 appears at about 10 s). Writing the block then hangs until the unit's 90 s timeout, and `systemd-rfkill.service` fails. With Wi-Fi unblocked there's nothing to write and it doesn't happen. So:

- `systemd-rfkill-couch.conf` removes the saved Wi-Fi state (`/var/lib/systemd/rfkill/*:wlan`) before systemd-rfkill starts. Without a saved state systemd-rfkill leaves that radio alone. Bluetooth's saved state stays, and is still restored.
- `70-couch-wifi.rules` starts `couch-wifi-boot.service` when wlan0 appears, once the chip is up. `couch-wifi-boot` reads NetworkManager's state file and, if it says `WirelessEnabled=false`, soft-blocks the Wi-Fi radio. It never unblocks anything and always exits 0.

The other ways considered, and why not:

- **Order or delay systemd-rfkill until wlan is up.** It's socket-activated by `/dev/rfkill` and restores each radio as it appears, so there's nothing to order it after: wlan0 isn't a dependency it can wait for. Delaying the whole unit would delay Bluetooth's restore too, and couchd powers Bluetooth on at start.
- **`systemd.restore_state=0`** on the kernel command line stops systemd-rfkill from restoring anything, Bluetooth included. Pi OS starts Bluetooth soft-blocked, so it would be off after every boot.
- **`dtoverlay=disable-wifi`** in `config.txt` removes the Wi-Fi chip altogether. It's the most thorough off, but every change needs a reboot, so it can't sit behind a toggle in Settings. It's still the right choice for a Pi that will never use Wi-Fi.

To check, on the Pi:

```bash
nmcli radio wifi
rfkill list
systemctl --failed
journalctl -b -u couch-wifi-boot -u systemd-rfkill
```

## Boot

From power-on the TV stays black until the Couch splash, then the launcher. The firmware's rainbow square is off (`disable_splash=1`). The kernel prints nothing on the TV: it gets `quiet loglevel=0`, no cursor, and its console moves from tty1 to tty3. Ctrl+Alt+F3 on a USB keyboard shows it.

The splash comes from the Raspberry Pi kernel's fullscreen logo (`fullscreen_logo=1 fullscreen_logo_name=couch-splash.tga`), not Plymouth. The kernel draws it as soon as the firmware's framebuffer appears, about a second after power-on, so it doesn't wait for any userspace. The kernel only draws it on that firmware framebuffer, though. About 6 s in, vc4 (the real display driver) takes the screen over with a framebuffer of its own, and the kernel won't draw a logo on a framebuffer that belongs to a module. So a udev rule starts `couch-fbsplash.service` the moment vc4's framebuffer appears, and `couch-fbsplash` draws the same TGA the same way: centred, the rest filled with its top-left pixel. The splash then stays until cage takes the screen. At a reboot or power-off the same unit stops just after couch-session, once cage has let go of the screen, and draws the splash again; it stays until the Pi resets. That redraw copies the frame kept in `/run` from the boot, so it takes a fraction of a second. The launcher's Startup screen (#19) draws the same lockup on the same pixels, so the splash seems to gain a progress bar. `logo.nologo` must stay off the command line: it hides this logo too.

The kernel uses the TV's EDID mode, 1920×1080 at 60 Hz, and adds no overscan border (`disable_overscan=1`). Set the TV not to crop either; see [the README](../README.md#the-tv). If a TV ever reports the wrong mode, `video=HDMI-A-1:1920x1080@60` on the command line forces it.

`couch-session.service` doesn't reset or clear tty1 when it starts (no `TTYReset`, no `TTYVTDisallocate`), so the splash stays up through cage's start. With the console on tty3, nothing else ever writes to tty1.

cloud-init is turned off once the card's first boot is done (`/etc/cloud/cloud-init.disabled`). It only sets up the user and SSH key on that first boot, and otherwise costs about 2.5 s of every boot. The network doesn't come from it: NetworkManager brings up eth0 by itself.

`scripts/boot-timing.sh` reads the last boot's milestones from the Pi's logs: when the splash could draw, when cage started, and when the launcher first painted. Run it with `ssh couch.local 'bash -s' < scripts/boot-timing.sh`.

To check what's set: `/opt/couch/bin/couch-bootfiles show`. `install.sh --uninstall` puts both files back the way they were. If the Pi ever doesn't boot, put the card in a PC: the boot partition has `cmdline.txt.couch-orig` and `config.txt.couch-orig`, the files as they were before Couch. Copy them over `cmdline.txt` and `config.txt`.
