# Security policy

PirateFinder parses untrusted disk images and archives, downloads files from
third-party websites, and writes to physical media through the Greaseweazle
host tool. Security reports are welcome even though the application is
intended for a single local GNOME user.

## Supported versions

| Version | Security fixes |
| --- | --- |
| Current `main` branch | Yes |
| Latest published release | Yes |
| Older tags and unmaintained branches | No |

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability. Use GitHub's private
[Report a vulnerability](https://github.com/peteclarke-del/PirateFinder/security/advisories/new)
workflow. If that workflow is unavailable, contact the repository owner
privately using the address on the maintainer's GitHub profile. Include the
affected version or commit, host distribution, a minimal reproduction using
generated media you may lawfully share, and sanitised logs.

You should receive an acknowledgement within five working days, and the date
of disclosure is agreed with the reporter. These are response targets, not a
service-level agreement.

Examples of what to report:

- command or argument injection into `gw` or other subprocesses;
- unsafe parsing of disk images, archives or downloaded pages;
- path traversal from archive member names or downloaded file names;
- a download kept without matching the catalogue hash, or a catalogue update
  installed without matching its published SHA-256;
- unintended access to host files outside the library and download folders;
- a floppy written without the insert-disk confirmation;
- vulnerable dependencies, including the bundled Greaseweazle host tools.

Use generated media and disposable floppies for research. Do not test systems
or data you do not own, and do not run load tests against the sites the
catalogue is built from.

There is no bug bounty.
