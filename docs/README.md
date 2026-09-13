# PirateFinder documentation

## Using PirateFinder

| Document | For |
| --- | --- |
| [User Guide](USER_GUIDE.md) | Every part of the application, with screenshots. The same text opens in the application from User Guide in the main menu, or with F1. |
| [Installation](INSTALLATION.md) | Installing the release package, the device rule, running alongside Greaseweazle-GUI, where files are kept, NAS folders, upgrading, removing and running from source. |
| [Troubleshooting](TROUBLESHOOTING.md) | The messages PirateFinder shows when something goes wrong, what they mean and what to check. |
| [Privacy](PRIVACY.md) | Every network request the application makes and every file it keeps. |
| [Format support](FORMAT_SUPPORT.md) | How each image format is prepared and written, what is refused and why, and how images are matched to the catalogue. |
| [Support](../SUPPORT.md) | What to include when asking for help. |

## The catalogue

| Document | For |
| --- | --- |
| [Data sources](DATA_SOURCES.md) | Every source the catalogue and the details pane read, what is taken from each, its terms and its request limits. |
| [Catalogue](CATALOGUE.md) | Building the catalogue, how records are merged into disks, its tables, how search works and how to add a series. |

## Developing PirateFinder

| Document | For |
| --- | --- |
| [Design](DESIGN.md) | How the parts fit together, the runtime interfaces, and the Find screen, details pane and virus handling. |
| [Current status](CURRENT_STATUS.md) | What works now and what is not done yet. |
| [Roadmap](../ROADMAP.md) | The planned work, stage by stage. |
| [Release process](RELEASING.md) | Publishing catalogue and application releases. |
| [Contributing](../CONTRIBUTING.md) | The development workflow, the checks to run, the documentation and screenshot tools, and the rules for new catalogue sources. |

## Project

| Document | For |
| --- | --- |
| [README](../README.md) | What PirateFinder is, in one page. |
| [Security policy](../SECURITY.md) | Reporting a vulnerability. |
| [Governance](../GOVERNANCE.md) | Roles in the project and how decisions are made. |
| [Code of conduct](../CODE_OF_CONDUCT.md) | Expected behaviour in the project. |
| [Third-party notices](../THIRD_PARTY_NOTICES.md) | Bundled and copied components and their licences. |
| [Notice](../NOTICE) and [Licence](../LICENSE) | The licences of the code (GPL-3.0-or-later) and the catalogue (CC BY-NC-SA 4.0). |

The screenshots in [images](images) are drawn from the real window by
`PYTHONPATH=src:. python3 -m piratefinder.ui.screenshot`, in light and dark.
Their pictures of menu screens are drawn for the documentation, and the
queue, summary and history show invented example sessions.
