## Summary

Describe the user-visible result and the technical boundary changed.

## Risk and compatibility

- Machines, disk series and image formats:
- Physical disk and source image safety:
- Catalogue schema or user database implications:
- Network behaviour (new hosts, request rates, terms of use):
- GNOME accessibility implications:

## Verification

- [ ] Python tests pass.
- [ ] Ruff check and format check pass.
- [ ] Python compilation and launcher syntax checks pass.
- [ ] Corrupt, truncated, cancellation and failure cases are covered where relevant.
- [ ] No disk image, archive or copyrighted fixture has been added; tests build synthetic images.
- [ ] Writing a floppy still requires the user to insert the disk and confirm.

List exact commands and relevant real-hardware evidence:

## Engineering review

- [ ] User-controlled paths and subprocess arguments remain bounded and validated.
- [ ] Downloads are checked against the catalogue hash before they are kept.
- [ ] Catalogue sources are cached, throttled and used within their terms.
- [ ] Changed controls remain keyboard-operable and clearly labelled.
- [ ] README, docs and in-app help describe what the code does.
