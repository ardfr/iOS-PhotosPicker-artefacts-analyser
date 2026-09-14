#!/usr/bin/env python3
"""
photospicker_report.py

Parses iOS 'File Provider Storage/photospicker' artefacts, decodes the
parameterised filenames, joins each UUID against Photos.sqlite (ZASSET /
ZGENERICASSET) and writes a CSV mapping each picker file back to the
original library asset.

Pure Python - standard library only (sqlite3, csv, argparse, pathlib).

Usage:
    python photospicker_report.py Photos.sqlite <photospicker_folder> -o report.csv
    python photospicker_report.py Photos.sqlite <photospicker_folder> -o report.csv ^
        --media-root "Z:\\extracted\\private\\var\\mobile\\Media" --hyperlinks

Notes:
  * Work on a COPY of Photos.sqlite and copy the -wal/-shm alongside it so
    unflushed records are included. The DB is opened read-only.
  * --media-root should point at the extracted /private/var/mobile/Media
    directory so original-file paths (and optional Excel hyperlinks) resolve.
"""

import argparse
import csv
import hashlib
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

COCOA_EPOCH = 978307200  # 2001-01-01 00:00:00 UTC

TYPE_DESC = {"1": "photo", "2": "live_photo", "3": "video"}
MODE_DESC = {"1": "current/original", "2": "compatibility/transcode"}

def parse_picker_name(name: str) -> dict:
    """Decode a photospicker filename into its parameters.

    Examples:
        uuid=AAAA...&code=001&library=1&type=1&mode=1&loc=true&cap=true.png
        uuid=AAAA...&library=1&type=1&mode=1&loc=true&cap=true&thumb=1.thumb.low
        uuid=AAAA...&code=001&library=1&type=3&mode=2&loc=true&cap=true&preset=1.mov
    """
    params = {}
    ext = ""
    segments = name.split("&")
    for i, seg in enumerate(segments):
        if "=" in seg:
            key, _, value = seg.partition("=")
        else:
            key, value = seg, ""
        if i == len(segments) - 1 and "." in value:
            # Last segment carries the extension chain after the first dot
            value, _, ext = value.partition(".")
        params[key.strip()] = value.strip()
    params["_ext"] = ext.lower()
    return params


def classify(params: dict) -> str:
    ext = params.get("_ext", "")
    if "thumb" in params or ext.startswith("thumb"):
        return "thumbnail"
    if ext == "pvt":
        return "export_livephoto_package"
    if params.get("type") == "3" or ext in ("mov", "mp4", "m4v"):
        return "export_video"
    return "export_image"

def open_db(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.as_posix()}?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True)
        con.execute("SELECT 1")
        return con
    except sqlite3.OperationalError as exc:
        sys.exit(f"[!] Could not open {path} read-only: {exc}\n"
                 f"    Copy the DB (with -wal/-shm) to a writable location "
                 f"and retry.")


def detect_asset_table(con: sqlite3.Connection) -> str:
    rows = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "ZASSET" in rows:
        return "ZASSET"
    if "ZGENERICASSET" in rows:  # iOS 13 and earlier
        return "ZGENERICASSET"
    sys.exit("[!] Neither ZASSET nor ZGENERICASSET found - is this "
             "Photos.sqlite?")


def table_columns(con: sqlite3.Connection, table: str) -> set:
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def cocoa_to_iso(value):
    if value is None:
        return ""
    try:
        return datetime.fromtimestamp(
            float(value) + COCOA_EPOCH, tz=timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (ValueError, OSError, OverflowError):
        return ""


def load_assets(con: sqlite3.Connection) -> dict:
    """Return {UUID: asset-info dict} for every row in the asset table."""
    table = detect_asset_table(con)
    cols = table_columns(con, table)

    def col(name, alias=None):
        alias = alias or name
        return f"A.{name} AS {alias}" if name in cols else f"NULL AS {alias}"

    aaa_cols = table_columns(con, "ZADDITIONALASSETATTRIBUTES") \
        if "ZADDITIONALASSETATTRIBUTES" in {
            r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")} \
        else set()
    orig_fn = ("AAA.ZORIGINALFILENAME AS ZORIGINALFILENAME"
               if "ZORIGINALFILENAME" in aaa_cols else
               "NULL AS ZORIGINALFILENAME")
    join = ("LEFT JOIN ZADDITIONALASSETATTRIBUTES AAA ON AAA.ZASSET = A.Z_PK"
            if aaa_cols else "")

    query = f"""
        SELECT A.ZUUID AS ZUUID,
               {col('ZDIRECTORY')},
               {col('ZFILENAME')},
               {col('ZDATECREATED')},
               {col('ZADDEDDATE')},
               {col('ZTRASHEDSTATE')},
               {col('ZTRASHEDDATE')},
               {col('ZLATITUDE')},
               {col('ZLONGITUDE')},
               {orig_fn}
        FROM {table} A
        {join}
    """
    con.row_factory = sqlite3.Row
    assets = {}
    for row in con.execute(query):
        uuid = row["ZUUID"]
        if uuid:
            assets[uuid.upper()] = dict(row)
    print(f"[*] Loaded {len(assets)} assets from {table}")
    return assets

def iso(ts):
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc
                                      ).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (ValueError, OSError, OverflowError):
        return ""


def excel_hyperlink(path: str, label: str) -> str:
    path = path.replace('"', '""')
    label = (label or path).replace('"', '""')
    return f'=HYPERLINK("{path}","{label}")'


def md5_file(path: Path) -> str:
    h = hashlib.md5()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def safe_copy(src: Path, dest_dir: Path) -> str:
    """Copy preserving timestamps; never overwrite silently."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    n = 1
    while dest.exists():
        dest = dest_dir / f"{src.stem}__{n}{src.suffix}"
        n += 1
    try:
        if src.is_dir():  # .pvt Live Photo packages are folders
            shutil.copytree(src, dest)
        else:
            shutil.copy2(src, dest)
        return str(dest)
    except OSError as exc:
        print(f"    [!] Copy failed: {src} -> {exc}")
        return ""


def main():
    ap = argparse.ArgumentParser(
        description="Map iOS photospicker artefacts back to Photos.sqlite "
                    "originals and write a CSV report.")
    ap.add_argument("photos_sqlite", type=Path,
                    help="Path to (a copy of) Photos.sqlite")
    ap.add_argument("picker_dir", type=Path,
                    help="Path to the extracted File Provider Storage/"
                         "photospicker folder")
    ap.add_argument("-o", "--output", type=Path,
                    default=Path("photospicker_report.csv"))
    ap.add_argument("--media-root", type=Path, default=None,
                    help="Extracted /private/var/mobile/Media directory, used "
                         "to build resolvable original-file paths")
    ap.add_argument("--hyperlinks", action="store_true",
                    help="Add Excel =HYPERLINK() columns for the picker file "
                         "and the original asset")
    ap.add_argument("--collect", type=Path, default=None, metavar="DIR",
                    help="Copy files of interest into DIR: full-res picker "
                         "exports, their matched originals (needs "
                         "--media-root), and picker files whose UUID is "
                         "missing from Photos.sqlite")
    ap.add_argument("--include-thumbs", action="store_true",
                    help="With --collect, also copy thumbnail files for "
                         "matched exports")
    args = ap.parse_args()

    if not args.photos_sqlite.is_file():
        sys.exit(f"[!] Not found: {args.photos_sqlite}")
    if not args.picker_dir.is_dir():
        sys.exit(f"[!] Not a directory: {args.picker_dir}")

    con = open_db(args.photos_sqlite)
    assets = load_assets(con)

    fields = [
        "picker_filename", "artefact_class", "uuid",
        "code", "library", "type", "type_desc", "mode", "mode_desc",
        "loc", "cap", "thumb", "preset", "extension",
        "picker_size_bytes", "picker_md5",
        "picker_created_utc", "picker_modified_utc",
        "in_photos_db", "asset_filename", "asset_directory",
        "asset_original_filename", "asset_relpath",
        "asset_created_utc", "asset_added_utc",
        "trashed_state", "trashed_date_utc", "latitude", "longitude",
    ]
    if args.hyperlinks:
        fields += ["link_picker_file", "link_original_asset"]
    if args.collect:
        fields += ["collected_picker_copy", "collected_original_copy"]

    entries = sorted(os.scandir(args.picker_dir), key=lambda e: e.name)
    collected_originals = {}
    rows, missing, uuids_seen = [], [], set()
    counts = {"thumbnail": 0, "export_image": 0, "export_video": 0,
              "export_livephoto_package": 0, "other": 0}

    for entry in entries:
        name = entry.name
        if not name.lower().startswith("uuid="):
            continue
        params = parse_picker_name(name)
        uuid = params.get("uuid", "").upper()
        uuids_seen.add(uuid)
        art_class = classify(params)
        counts[art_class] = counts.get(art_class, 0) + 1

        try:
            st = entry.stat()
            size = st.st_size
            created = iso(getattr(st, "st_birthtime", st.st_ctime))
            modified = iso(st.st_mtime)
        except OSError:
            size, created, modified = "", "", ""

        asset = assets.get(uuid)
        relpath = ""
        if asset and asset.get("ZDIRECTORY") and asset.get("ZFILENAME"):
            relpath = f"{asset['ZDIRECTORY']}/{asset['ZFILENAME']}"
        if not asset:
            missing.append((uuid, name, art_class))

        row = {
            "picker_filename": name,
            "artefact_class": art_class,
            "uuid": uuid,
            "code": params.get("code", ""),
            "library": params.get("library", ""),
            "type": params.get("type", ""),
            "type_desc": TYPE_DESC.get(params.get("type", ""), ""),
            "mode": params.get("mode", ""),
            "mode_desc": MODE_DESC.get(params.get("mode", ""), ""),
            "loc": params.get("loc", ""),
            "cap": params.get("cap", ""),
            "thumb": params.get("thumb", ""),
            "preset": params.get("preset", ""),
            "extension": params.get("_ext", ""),
            "picker_size_bytes": size,
            "picker_md5": ("" if entry.is_dir()
                           else md5_file(Path(entry.path))),
            "picker_created_utc": created,
            "picker_modified_utc": modified,
            "in_photos_db": "YES" if asset else "NO",
            "asset_filename": (asset or {}).get("ZFILENAME") or "",
            "asset_directory": (asset or {}).get("ZDIRECTORY") or "",
            "asset_original_filename":
                (asset or {}).get("ZORIGINALFILENAME") or "",
            "asset_relpath": relpath,
            "asset_created_utc": cocoa_to_iso((asset or {}).get(
                "ZDATECREATED")),
            "asset_added_utc": cocoa_to_iso((asset or {}).get("ZADDEDDATE")),
            "trashed_state": (asset or {}).get("ZTRASHEDSTATE", ""),
            "trashed_date_utc": cocoa_to_iso((asset or {}).get(
                "ZTRASHEDDATE")),
            "latitude": (asset or {}).get("ZLATITUDE", ""),
            "longitude": (asset or {}).get("ZLONGITUDE", ""),
        }
        # -180.0 is Photos' sentinel for "no location"
        if row["latitude"] in (-180.0, "-180.0"):
            row["latitude"] = row["longitude"] = ""

        if args.hyperlinks:
            row["link_picker_file"] = excel_hyperlink(
                str(Path(entry.path).resolve()), name)
            if relpath and args.media_root:
                orig = args.media_root / relpath
                row["link_original_asset"] = excel_hyperlink(
                    str(orig.resolve()), relpath)
            else:
                row["link_original_asset"] = ""

        if args.collect:
            row["collected_picker_copy"] = ""
            row["collected_original_copy"] = ""
            picker_path = Path(entry.path)
            is_export = art_class.startswith("export")
            if not asset:
                row["collected_picker_copy"] = safe_copy(
                    picker_path, args.collect / "missing_from_photos_db")
            elif is_export or (args.include_thumbs and asset):
                sub = "exports" if is_export else "thumbnails"
                row["collected_picker_copy"] = safe_copy(
                    picker_path, args.collect / sub)
                if is_export and relpath and args.media_root:
                    if relpath in collected_originals:
                        row["collected_original_copy"] = \
                            collected_originals[relpath]
                    else:
                        orig = args.media_root / relpath
                        if orig.exists():
                            dest = safe_copy(orig,
                                             args.collect / "originals")
                            collected_originals[relpath] = dest
                            row["collected_original_copy"] = dest
                        else:
                            print(f"    [!] Original not found on disk: "
                                  f"{orig}")

        rows.append(row)

    with open(args.output, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    # ---- summary -----------------------------------------------------------
    exports = [r for r in rows if r["artefact_class"].startswith("export")]
    print(f"\n[*] Wrote {len(rows)} rows -> {args.output}")
    print(f"    Distinct UUIDs:          {len(uuids_seen)}")
    print(f"    Full-res exports:        {len(exports)} "
          f"(images {counts['export_image']}, videos {counts['export_video']},"
          f" live photo pkgs {counts['export_livephoto_package']})")
    print(f"    Thumbnails:              {counts['thumbnail']}")
    print(f"    Not in Photos.sqlite:    {len(set(m[0] for m in missing))} "
          f"UUID(s)  <-- potential deleted/purged assets")
    for uuid, name, art in sorted(set(missing))[:20]:
        print(f"        {uuid}  ({art})")
    if len(set(m[0] for m in missing)) > 20:
        print("        ... (full list in CSV, filter in_photos_db = NO)")
    if args.collect:
        print(f"    Collected files copied to: {args.collect.resolve()}")


if __name__ == "__main__":
    main()
