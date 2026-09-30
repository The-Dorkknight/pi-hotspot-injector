# Security

If you find a security problem (for example a way to make the tool write to a drive it shouldn't, or to leak a password), please **don't** post it publicly. Use GitHub's *Report a vulnerability* button on the Security tab of this repository, or open an issue asking for a private contact without giving details.

Useful to know:
- The tool runs as root (via `pkexec`) while it works.
- Passwords are never written to the log.
- The hotspot password and the Pi login password are shown once, on screen, so you can write them down.
