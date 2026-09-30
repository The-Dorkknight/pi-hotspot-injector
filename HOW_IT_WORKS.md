# How it works

This page explains what Pi Hotspot Injector does to your SD card and your PC, step by step, so you can decide whether to trust it and understand what happens if something goes wrong. It's one Python file (`pi_injector.py`) with no hidden parts: everything below is in that file.

## The big picture

```
 Your Linux PC                                            The Pi
┌──────────────────────────────────────────────┐        ┌────────────────┐
│ 1 Write image  →  SD card holds Pi OS Lite    │        │                │
│ 2 Build Klipper → software installed ONTO it  │  card  │  boots, joins  │
│ 3 Hotspot       → Wi-Fi settings written      │ ─────► │  nothing, and  │
│ (Clone: copy the finished card to more cards) │        │  makes its own │
└──────────────────────────────────────────────┘        │  Wi-Fi network │
                                                          └────────────────┘
```

Everything happens on your PC, with the card in a USB reader. The Pi doesn't need to be switched on, and you don't need a keyboard or screen for it.

## Why it asks for your password

Writing to an SD card needs administrator rights. On start-up the tool re-launches itself through `pkexec` (the standard graphical password prompt) and keeps your normal screen and home folder, so the window still appears and files land in *your* folders, not root's. The tool doesn't install itself on your PC. The one thing it may install is the `qemu-user-static` emulator (through `apt-get`) the first time you use the builder or the Extras package installer.

## Tab 1: Write Image

1. **Choosing the drive.** The tool lists disks with `lsblk` and only offers a drive if *all* of these are true: it's removable (USB stick, USB card reader, or a built-in SD slot), it isn't your PC's own storage (no internal SATA/NVMe, no built-in eMMC, nothing holding your system, home folders or swap), it is sized **7–33 GB** (sold as 8–32 GB), and it isn't write-protected. Anything else is refused, and the *Why isn't my drive listed?* button says why.
2. **Two sanity checks.** The first shows the drive, its size, and what's on it now. The second asks "Are you reeeeley sure you want to overwrite disk…?" and only enables its button after you type the drive's name (e.g. `sdc`), so a reflex click or Enter can't get through.
3. **Re-check.** Just before writing, it checks the drive again (size, serial number, contents). If you swapped or unplugged it in between, nothing is written.
4. **Writing.** The image is unpacked on the fly (`.img`, `.xz`, `.gz`, `.bz2`, `.zip`, `.zst`) and written straight to the device. The tool opens the device **exclusively**: the kernel refuses if anything still has it mounted. Data is flushed to the card every 64 MB, and the last 1 MB is wiped so leftover partition tables from an old use of the card can't confuse anything.
5. **Verify.** The tool reads the card back and compares a checksum with what it wrote. This catches bad cards, flaky readers and counterfeit cards that quietly drop data. The same open handle is kept for writing and verifying, so your desktop can't auto-mount the card in between and change a few bytes.

**The download button** fetches the official *Raspberry Pi OS Lite (32-bit)* from `downloads.raspberrypi.com`, then checks it against the SHA-256 checksum the Raspberry Pi Foundation publishes next to it. The download is saved in 10 MB checkpoints and resumes after a dropout; see [Downloads that survive dropouts](#downloads-that-survive-dropouts).

## Tab 2: Build Klipper

The idea: instead of using a ready-made Klipper image (which may be built for the wrong CPU), start from the plain official image and install Klipper into it from your PC.

1. **Preflight (read-only).** The card must be a never-started Raspberry Pi OS card (fresh, or one of this tool's own unfinished builds). It refuses a PC installer ISO, a card that has already been booted, or a card that already has Klipper (e.g. MainsailOS). It also runs `e2fsck -n` to check the filesystem *before* anything changes.
2. **Grow the system partition.** A freshly written image is small; the Pi normally enlarges it on its first start. The build needs room now, so the tool does the same (`sfdisk`, `e2fsck`, `resize2fs`). The disk ID is preserved so the Pi's boot settings still match.
3. **Chroot.** The card's system partition is mounted on your PC and the tool `chroot`s into it. Because the card holds ARM programs and your PC is (almost certainly) x86, it copies in a static `qemu` emulator. It sets the emulated CPU to the **Pi Zero's ARM1176**, so anything compiled inside is built for the *oldest* Pi. Every newer Pi can run that code too, which is why one card works in every Pi.
4. **Install.** Inside the chroot, a script installs the packages (apt), then clones **Klipper**, **Moonraker**, **mainsail-config** and (optionally) **Crowsnest** straight from their own GitHub repositories, builds their Python environments, and unpacks the latest **Mainsail** release. It sets up the user, hostname, SSH, nginx, services and Moonraker's update manager, and turns off Raspberry Pi OS's first-boot "create a user" wizard and cloud-init so they don't rename your user and break Klipper's paths.
5. **Check.** Every compiled file is scanned to confirm it only uses instructions the Pi Zero's CPU has. Then **Moonraker is test-started** on the emulated Pi to prove it runs.
6. **Clean up.** Everything is unmounted and flushed. If you cancel or the network drops, the card is tidied up safely and the build can simply be started again; it carries on where it stopped.

Your **login password** is passed to the chroot through a temporary file, hashed, and never written to the log.

## Tab 3: Hotspot Setup

Writes the settings that make the Pi broadcast its own Wi-Fi network on first start, then re-checks the card is compatible.

- **Recommended backend, hostapd + dnsmasq:** `hostapd` makes the network, `dnsmasq` hands out addresses, and NetworkManager is told to leave `wlan0` alone. A small service gives `wlan0` the fixed address (default `192.168.50.1`). This is more reliable than NetworkManager's own AP mode on the Pi Zero W's Wi-Fi chip.
- **Alternative, NetworkManager:** a NetworkManager access-point profile plus an optional watchdog that retries bringing it up.
- **Always:** WPA2 password, your country code (needed for Wi-Fi to be legal and to work), and Wi-Fi unblocked at boot.

A random password is made for you each time. The hotspot address defaults to `192.168.50.x` because nearly every home router uses `192.168.0.x` or `192.168.1.x`, and clashing with one makes things fail in confusing ways.

## Clone & Backup

- **Save a card:** the card is opened **read-only** and copied block by block into a file (empty areas take no disk space). With *Shrink* on, the copy's filesystem is checked and shrunk to what's used plus 512 MB, and the file is cut off after it, so it fits any card of similar size. The card itself is never touched. Cancelling deletes the unfinished file.
- **Write a copy:** the same writer and safety checks as tab 1, then the system partition is grown to fill the new card. With *Make it a separate Pi* ticked, the copy gets its own hostname, a blank machine ID (systemd makes a fresh one on first start) and its SSH host keys are deleted and regenerated on first start. Two clones never share an identity.

## Downloads that survive dropouts

Big downloads over flaky Wi-Fi are the most common way to lose an hour, so downloads work in checkpoints:

- Received data is flushed to disk and its position saved every **10 MB**, in a small `.part.json` file next to the `.part` file.
- If the connection drops, the position reached is saved and the download reconnects with an HTTP `Range` request and carries on. Waits between tries grow (5 s up to 60 s). After about 5 minutes with no progress it *pauses* and tells you; press Download again to continue, even after a restart.
- After a crash or power cut, the file is cut back to the last saved checkpoint first, since data written after it may be half-finished.
- If the file changed on the server meanwhile, or the server can't resume, it starts again from zero; it never joins two different files together.
- Finally the whole file is checked against its published checksum.

## The Pi Zero fix (Extras)

Some ready-made images contain programs built for newer Pi processors. On a Pi Zero, Zero W or Pi 1 they crash with "Illegal instruction". The tool reads each program's ARM build attributes (`.ARM.attributes`) to see what CPU it needs, and for Python packages that need a newer CPU it rebuilds them from source inside the emulated-Pi-Zero chroot, then tests each one there.

## What it does NOT do

- It doesn't send anything anywhere, and has no telemetry. It only connects to `raspberrypi.com` (image download), GitHub (Klipper, Moonraker, Mainsail, Crowsnest), the Raspberry Pi and Debian package servers, and PyPI/piwheels (Python packages) while building.
- It doesn't touch any drive you didn't choose, or any drive outside the 8–32 GB removable window.
- It doesn't log passwords.
- It doesn't configure your printer. After the first boot, Mainsail shows a Klipper error until you add your printer's `printer.cfg`; that's expected.

## Where things are

| What | Where |
|---|---|
| The whole program | `pi_injector.py` |
| Log file (no passwords) | `~/pi_injector.log` |
| Downloaded images | `~/Downloads` |
| Saved card images | `~/Pi-images` (you can change it) |
| Start-up launcher | `PiInjector.desktop` |
