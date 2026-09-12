# Format support

PirateFinder writes disks by running the Greaseweazle host tool, `gw write`.
Before each write it prepares the image (`src/piratefinder/images/prepare.py`):
containers are decoded to raw sectors, the disk layout is worked out, and a
file and format that `gw` accepts as they stand are written to a temporary
folder. Your own image files are never changed. Every conversion is listed in
the session summary, and an image that is refused comes with the reason.

## Writing

| Input | Handling |
| --- | --- |
| `.adf`, 80 cylinders | Written as it is, with `amiga.amigados`, or `amiga.amigados_hd` for a 1.76 MB high-density image. |
| `.adf`, 81 to 84 cylinders | Written with a generated disk definition that names the real cylinder count, so no cylinder is dropped. |
| `.adf`, fewer than 80 cylinders | Padded to 80 cylinders; the missing cylinders are written blank and the summary says so. |
| Extended ADF | Refused. It stores raw tracks for copy-protected disks, which Greaseweazle cannot write. Choose an IPF or a standard ADF dump. |
| `.dms` | Decoded to ADF in memory with the vendored DiskMasher decoder, which checks every track's checksum, then written as ADF. |
| `.adz`, `.gz` | Decompressed, then handled by what is inside (normally an ADF). An image compressed twice is refused. |
| `.st` | The layout comes from the boot sector, checked against the file size. The standard 80-cylinder layouts use `atarist.360` to `atarist.880` and `ibm.1440`. Any other layout, such as 82 or 83 cylinders or 11 sectors on one side, gets a generated disk definition so no track is dropped. When the boot sector does not describe the disk, the layout is taken from the file size and the summary says so. |
| `.msa` | Unpacked to raw sectors, then written exactly as an `.st`, so the layout handling is identical. |
| `.stx` | Converted to `.st` only when the Pasti image records no copy protection. A protected STX is refused, because a sector image cannot hold the protection and Greaseweazle cannot write STX files; choose an IPF, SCP or HFE dump instead. |
| `.ipf` | Written directly when the SPS CAPS library (`libcapsimage`) is installed. It cannot be shipped with PirateFinder; without it the image is refused with a pointer to softpres.org. |
| `.scp`, `.hfe` | Written directly as flux. Greaseweazle cannot read back and verify a flux write, so the result is reported as written, not verified. |
| Inside `.zip` or `.7z` | The disk image member is read into memory, then handled as above. Reading `.7z` needs the `7z` command from the 7zip package. |

### Generated disk definitions

`gw write` writes exactly the cylinders its format names. Writing an
82-cylinder ST image with the built-in `atarist.800` format silently loses
cylinders 80 and 81, and many menu disks were formatted with extra cylinders
to fit more on them. For any layout that is not one of the built-in 80-cylinder
formats, PirateFinder generates a `--diskdefs` file with the real cylinder,
head and sector counts. The track parameters (gap, data rate, sector size) are
copied from Greaseweazle 1.23's own `atarist.*`, `ibm.1440` and
`amiga.amigados` definitions, so a generated format differs from the built-in
one only in its geometry. Layouts beyond 86 cylinders, other sector sizes and
sector counts with no matching track parameters are refused.

### Platform

The platform the catalogue lists for a disk is checked against the image. When
they disagree, the image wins and the summary says so. An image that could be
for either machine is written the same way either way.

## Write results

Greaseweazle reports some hardware failures, such as a write-protected disk,
as `Command Failed` while still exiting with status zero. PirateFinder reads
the output of `gw write` rather than only its exit status, and reports:

| Result | Meaning |
| --- | --- |
| Verified | Every track was written and read back successfully. |
| Written | The disk was written, but `gw` could not verify it (flux images). |
| Write-protected | The disk's write-protect hole is open. Close it and try again. |
| No disk | No disk was found in the drive (no index pulse, or track 0 not found). |
| Failed | Any other hardware error, a track that failed verification, a fatal error, or output that does not confirm the write. The failed tracks are listed. |
| Cancelled | Writing was stopped. The disk is incomplete. |
| Skipped | You chose Skip at the insert-disk prompt. |
| Unavailable | No usable image could be found, downloaded or prepared for the disk. |

The drive (A, B or 0 to 3), the number of retries per track and whether to
erase each track before writing it are set in Preferences.

## Reading and matching

The library scanner and the download check read these formats to identify a
disk and match it to the catalogue:

| Format | Matched by |
| --- | --- |
| `.st`, `.adf` | Hashes of the raw sectors, which are the hashes TOSEC lists. |
| `.msa`, `.dms`, `.adz`, unprotected `.stx` | Decoded to raw sectors first, so they match the TOSEC entry for the same disk whatever the container. The file's own hashes are kept too. |
| `.ipf`, `.scp`, `.hfe`, protected `.stx` | File hashes only; there are no raw sectors to hash. |
| `.zip`, `.7z`, `.gz` | Each disk image inside is read and matched as above. One level of nesting is followed, such as a 7z holding one zip per disk. |

Matching tries raw MD5, raw SHA-1, raw CRC32 with size, then the file SHA-512
and file MD5, because Atari Legend lists the SHA-512 of its `.msa` files.
Files that match nothing stay in the library and can be searched by file
name, volume label and the names of the files on the disk, read from Atari
TOS (FAT12) and AmigaDOS directories.
