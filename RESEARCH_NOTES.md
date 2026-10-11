# Research and field notes

What was found out while building Pi Hotspot Injector, and how sure each finding is. These are working notes, kept so the reasons behind the design aren't lost. For what the tool does step by step, see [HOW_IT_WORKS.md](HOW_IT_WORKS.md).

Each note says where it comes from:

- **Real run**: seen on the author's own hardware (a Linux Mint laptop, a Pi Zero W, a "32 GB" card).
- **Test rig**: seen in automated tests on a PC, using stand-ins for the card (disk image files, a test Linux system). Not confirmed on real hardware.
- **Not checked**: an assumption or a report that hasn't been verified.

## 1. Why Moonraker crashed on the Pi Zero W

- **Symptom (real run):** on a Pi Zero W with a ready-made MainsailOS card, Moonraker kept restarting. The service log showed `status=4/ILL` ("Illegal instruction").
- **Cause:** the image contained compiled software built for a newer ARM processor (ARMv7) than the Pi Zero, Zero W and Pi 1 have (ARMv6, the ARM1176 chip). The older chip meets an instruction it doesn't have and the program is killed.
- **How the tool detects it:** every compiled file on the card carries a small table (`.ARM.attributes`) saying which processor it was built for. The tool reads that table and flags files needing more than ARMv6 (for example NEON or Thumb-2).
- **What didn't work (real run):** repairing the MainsailOS card in place. On a card that had already been modified many times, Moonraker still failed afterwards.
- **What did work (real run):** starting from plain Raspberry Pi OS Lite (32-bit) and installing Klipper, Moonraker and Mainsail onto it from the PC, inside an emulator set to the Pi Zero's own processor (`QEMU_CPU=arm1176`). One real build ended with 123 compiled files checked and 0 problems, Moonraker (v0.11.0) started and answered in the test start, and the card then ran in the Pi Zero W.

## 2. The base image

- **Use Raspberry Pi OS Lite, 32-bit ("armhf").** The image used in the real run was `2026-09-15-raspios-trixie-armhf-lite.img.xz`, about 530 MB to download and about 2.8 GB once written.
- **Not "Raspberry Pi Desktop for PC and Mac".** That download is a PC installer (an ISO) and can't start on a Pi. It's easy to pick by mistake, so the tool recognises it and says so.
- **The desktop edition works but is heavy** on a Pi Zero (512 MB of memory). The tool sets such a card to start without the desktop.
- **Python packages (real run):** that image has Python 3.13. Ready-built packages for the Pi Zero's processor came from piwheels (files ending `linux_armv6l.whl`), so little had to be compiled.
- **`python3-msgspec` isn't in that image's package list.** It's only a speed-up; pip installs msgspec anyway. An early version warned about this needlessly.
- **Build time (real run):** one full build on the laptop, with a good connection, took about 8 minutes. The "1 to 2 hours" in the README is a cautious figure for slow connections, slow PCs, and builds that have to compile packages. It hasn't been measured on other machines.

## 3. The hotspot

- **hostapd + dnsmasq is the recommended method.** In earlier testing on the Pi Zero W, NetworkManager's own access-point mode was unreliable on newer (trixie-based) images. The exact cause was not pinned down in these notes.
- **Default address `192.168.50.1`.** Nearly every home router uses `192.168.0.x` or `192.168.1.x`; using the same range makes things fail in confusing ways.
- **The hotspot worked on a Pi Zero W (real run)** with a card built by the tool.
- **Two easy mistakes (real run):**
  - `pi.local` didn't open because `pi` was the login user. The network name is the *hostname* (default `klipper`, so `http://klipper.local`). Phones often can't open `.local` names at all; the number always works.
  - The phone joined without asking for a password. It had remembered the password from an earlier card with the same hotspot name. "Forget network" on the phone fixes it. The settings the tool writes do require a password (WPA2).

## 4. Writing and checking cards

- **Desktops mount a new card by themselves.** That changes a few bytes on it, which can make a read-back check fail for no real reason. The tool keeps one exclusive hold on the card through writing *and* checking (test rig).
- **"32 GB" cards aren't the same size.** The card in the real run held 31.3 GB. A copy of one card may not fit another brand's, which is why Clone & Backup shrinks the copy to what's used.
- **The password prompt hides the screen settings.** `pkexec` removes the settings a window needs to appear; the tool passes them through.
- **Linux holds writes in memory.** A log file copied to a USB stick on Linux Mint was empty on another computer until the stick was ejected properly first (real run). Not a fault in the tool.

## 5. Downloads on a poor connection

- **Symptom (real run):** a build stopped with "Could not resolve host: github.com" when the Wi-Fi dropped.
- **First idea:** save a checkpoint every 10 MB and, after a drop, go back to the last checkpoint.
- **Problem found (test rig):** if the connection drops more often than every 10 MB, the download never gets anywhere.
- **What's used now:** save at the exact point of the drop and carry on from there. Going back to the last 10 MB checkpoint is kept for crashes and power cuts, where the last data written may be half-finished. Tested against a local test server that fakes dropouts; not yet seen recovering from a real dropout on the real download.

## 6. Clone & Backup

- **Shrinking (test rig):** a 3 GB test card became a 1.4 GB file and fitted a smaller card; the original card was unchanged byte for byte.
- **Making a copy its own Pi (test rig):** new hostname, blank machine ID, SSH keys removed and remade on first start.
- **"Grow to fill the card" on a copy:** uses the same code that enlarged the card in the real build, but in the copy flow it has only been run on stand-ins. The author reported the version containing this tab working; which parts were exercised wasn't recorded.

## 7. The KlipPlug option (2.4.x)

- KlipPlug is a separate project by the same author. The option installs it as its own service on port 7130.
- **Test rig only:** tested with a stand-in for the card. Not yet built onto a real SD card or started on a real Pi. KlipPlug itself has not been run on a physical printer.
- **Not checked:** how well it runs beside Klipper in a Pi Zero W's 512 MB of memory.
- **A local copy goes onto the card without its `.git` folder**, because a clone's settings can hold a login token.
- When checked on 7 October 2026, `https://github.com/The-Dorkknight/klipplug` could not be downloaded without logging in (not published yet, or private). Until it is public, the option needs a `klipplug` folder or zip placed next to the tool; without one the build finishes without KlipPlug and says so.

## 8. Still open

- Other Pi models (Pi 3, 4, 5, Zero 2 W) with a card built by this tool: expected to work, not tried.
- Other Linux desktops and card readers.
- Crowsnest (webcam) on a real Pi.
- The 64-bit image path.
- Recovery from a real network dropout during the image download.
