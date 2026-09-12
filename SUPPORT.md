# Support

Search the README, the documents under `docs/` and existing issues before
opening a report. Include the PirateFinder version, the catalogue build date,
the Linux distribution, the Greaseweazle model, host-tool version and drive
type, the disk label, where the image came from, and sanitised logs. The
catalogue build date is shown in Preferences, on the Catalogue page. The
**Diagnostic Log** in the main menu holds the Greaseweazle output and other
messages from the current session and can be copied; remove private paths
before posting it.

For a write problem, also include the image format and the output of the
failed step from the session summary. PirateFinder reports hardware failures
that `gw` prints without a failing exit status, such as a write-protected or
missing disk, as their own result; check the summary before assuming the
image is bad. See [Format support](docs/FORMAT_SUPPORT.md) for the formats
that can be written and the ones that are refused.

For a disk whose contents are wrong or missing, use the catalogue correction
template rather than a bug report. The catalogue is built from public sources
listed in [Data sources](docs/DATA_SOURCES.md), and a correction is most useful
when it says where the right information comes from.

For a download that fails, check whether the provider is switched on in
Preferences and whether the site is reachable in a browser. PirateFinder keeps
a download only when it matches the catalogue hash, so a mirror that serves a
different dump is reported as a mismatch rather than written.

Do not upload disk images, archives, credentials, private paths or device
identifiers, and do not post links to places that host disk images. Use the
issue templates for reproducible defects, catalogue corrections and feature
requests.

Security reports follow [SECURITY.md](SECURITY.md) and must not be filed as
public support issues. Conduct reports follow
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
