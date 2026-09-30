# Third-party software

This tool is licensed under the [PolyForm Noncommercial License 1.0.0](LICENSE.md). **That licence applies only to this tool's own code.**

The tool does not include any of the software below. It downloads it, at your request, from each project's own servers when you build a card. That software stays under its own licences, which are different from this one (mostly the GNU GPL) and which allow things this licence doesn't. Please read them, especially if you plan to share or sell what you build.

| Software | Used for | Where to find its licence |
|---|---|---|
| Raspberry Pi OS | The base image | <https://www.raspberrypi.com/software/> (a Debian-based collection of many licences) |
| Klipper | 3D-printer firmware host | <https://github.com/Klipper3d/klipper> |
| Moonraker | Klipper's web API | <https://github.com/Arksine/moonraker> |
| Mainsail and mainsail-config | The web interface | <https://github.com/mainsail-crew/mainsail>, <https://github.com/mainsail-crew/mainsail-config> |
| Crowsnest and ustreamer | Optional webcam streaming | <https://github.com/mainsail-crew/crowsnest>, <https://github.com/pikvm/ustreamer> |
| QEMU (user-mode) | Runs ARM programs on your PC during the build | <https://www.qemu.org/> |
| hostapd, dnsmasq, nginx, NetworkManager | Hotspot and web server on the Pi | Distributed as Debian packages with their own licences |

The tool itself uses only the Python standard library and standard Linux tools (`lsblk`, `sfdisk`, `e2fsck`, `resize2fs`, `chroot`, `xz`, and so on).

This project is not made by, affiliated with or endorsed by Raspberry Pi Ltd, the Klipper, Moonraker, Mainsail or Crowsnest projects, or the Raspberry Pi Foundation. "Raspberry Pi" is a trademark of Raspberry Pi Ltd.
