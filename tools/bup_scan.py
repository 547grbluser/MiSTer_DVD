#!/usr/bin/env python3
"""bup_scan.py -- library census for the .BUP fallback (audit item 8).

For every DVD-Video .iso under $DVD_ISO_DIR (or the paths given), and for the VMG
plus every VTS, records:

  * whether the IFO and the BUP directory records exist;
  * whether each carries its magic at byte 0 ("DVDVIDEO-VMG" / "DVDVIDEO-VTS");
  * whether IFO and BUP are the same size and byte-identical (and, if not, the
    first differing sector);
  * VTSs that have an IFO but no title VOB (the reader keeps no gmem row for them).

Why: the fabric falls back to the BUP when an IFO's sector-0 magic is wrong
(docs/dvd_nav.md "IFO header gate"), so the magic must be present on every good
IFO for that to be a safe trigger; and the Main's sector mirror
(docs/physical_disc.md) serves BUP sector k for IFO sector k, which is only right
when the two files are the same length and identical.

These are rips. A ripper may rewrite or drop the BUP, so the library can only
bound what pressed discs carry, not prove it.

Usage:  tools/bup_scan.py [--json out.json] [ISO_OR_DIR ...]
        (no paths: walks $DVD_ISO_DIR)
"""
import argparse
import json
import os
import struct
import sys

SEC = 2048
MAGIC = {"VMG": b"DVDVIDEO-VMG", "VTS": b"DVDVIDEO-VTS"}


def read_sec(f, lba, n=1):
    f.seek(lba * SEC)
    d = f.read(n * SEC)
    return d + b"\0" * (n * SEC - len(d))


def read_dir(f, dlba, dlen):
    nsec = (dlen + SEC - 1) // SEC
    buf = read_sec(f, dlba, nsec)
    out = []
    for s in range(nsec):
        p, end = s * SEC, (s + 1) * SEC
        while p < end:
            rl = buf[p]
            if rl == 0:
                break
            ext = struct.unpack("<I", buf[p + 2:p + 6])[0]
            dl = struct.unpack("<I", buf[p + 10:p + 14])[0]
            fl = buf[p + 25]
            nl = buf[p + 32]
            out.append((buf[p + 33:p + 33 + nl].upper(), ext, dl, fl))
            p += rl
    return out


def walk(f):
    """-> {stem: {"IFO": (lba, len), "BUP": ..., "nvob": n}} or None."""
    pvd = None
    for lba in range(16, 48):
        d = read_sec(f, lba)
        if d[1:6] != b"CD001":
            return None
        if d[0] == 1:
            pvd = d
            break
        if d[0] == 255:
            break
    if pvd is None:
        return None
    root_lba = struct.unpack("<I", pvd[158:162])[0]
    root_len = struct.unpack("<I", pvd[166:170])[0]
    vdir = None
    for nm, ext, dl, fl in read_dir(f, root_lba, root_len):
        if nm == b"VIDEO_TS" and (fl & 2):
            vdir = (ext, dl)
    if not vdir:
        return None
    sets = {}
    for nm, ext, dl, fl in read_dir(f, *vdir):
        if fl & 2:
            continue
        base = nm.split(b";")[0].decode("latin-1")
        if base in ("VIDEO_TS.IFO", "VIDEO_TS.BUP"):
            sets.setdefault("VMG", {})[base[-3:]] = (ext, dl)
        elif (len(base) == 12 and base.startswith("VTS_") and base[4:6].isdigit()
              and base[6] == "_" and base[7].isdigit() and base[8] == "."):
            stem = "VTS_" + base[4:6]
            s = sets.setdefault(stem, {})
            sfx = base[9:]
            if base[7] == "0" and sfx in ("IFO", "BUP"):
                s[sfx] = (ext, dl)
            elif sfx == "VOB" and base[7] != "0":
                s["nvob"] = s.get("nvob", 0) + 1
    return sets


def scan_iso(path):
    with open(path, "rb") as f:
        sets = walk(f)
        if sets is None:
            return None
        rows = []
        for stem in sorted(sets, key=lambda s: (s != "VMG", s)):
            s = sets[stem]
            kind = "VMG" if stem == "VMG" else "VTS"
            r = {"set": stem, "ifo": "IFO" in s, "bup": "BUP" in s,
                 "nvob": s.get("nvob", 0)}
            data = {}
            for k in ("IFO", "BUP"):
                if k in s:
                    lba, ln = s[k]
                    data[k] = read_sec(f, lba, (ln + SEC - 1) // SEC)[:ln]
                    r[k.lower() + "_magic"] = data[k][:12] == MAGIC[kind]
                    r[k.lower() + "_len"] = ln
                    r[k.lower() + "_lba"] = lba
            if len(data) == 2:
                a, b = data["IFO"], data["BUP"]
                r["same_len"] = len(a) == len(b)
                r["identical"] = a == b
                if a != b:
                    n = min(len(a), len(b))
                    i = next((j for j in range(n) if a[j] != b[j]), n)
                    r["first_diff_sec"] = i // SEC
                r["same_lba"] = s["IFO"][0] == s["BUP"][0]
            rows.append(r)
        return rows


def iso_paths(args):
    roots = args or [os.environ.get("DVD_ISO_DIR", "")]
    if roots == [""]:
        sys.exit("bup_scan: give ISO paths/dirs or set DVD_ISO_DIR")
    for r in roots:
        if os.path.isfile(r):
            yield r
            continue
        for dp, _, fns in os.walk(r):
            for fn in sorted(fns):
                if fn.lower().endswith(".iso"):
                    yield os.path.join(dp, fn)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--json", help="write every per-set row here")
    a = ap.parse_args()

    tot = {"isos": 0, "not_dvd": 0, "sets": 0, "ifo": 0, "bup": 0, "pair": 0,
           "ifo_magic_bad": 0, "bup_magic_bad": 0, "len_differ": 0,
           "not_identical": 0, "same_lba": 0, "isos_no_bup": 0,
           "isos_all_bup": 0, "ifo_no_titlevob": 0, "bup_no_ifo": 0}
    odd = []
    allrows = {}
    for p in iso_paths(a.paths):
        tot["isos"] += 1
        try:
            rows = scan_iso(p)
        except OSError as e:
            odd.append((p, "read error: %s" % e))
            continue
        if rows is None:
            tot["not_dvd"] += 1
            continue
        allrows[p] = rows
        nb = 0
        for r in rows:
            tot["sets"] += 1
            tot["ifo"] += r["ifo"]
            tot["bup"] += r["bup"]
            nb += r["bup"]
            if r["ifo"] and not r["ifo_magic"]:
                tot["ifo_magic_bad"] += 1
                odd.append((p, "%s IFO magic bad" % r["set"]))
            if r["bup"] and not r["bup_magic"]:
                tot["bup_magic_bad"] += 1
                odd.append((p, "%s BUP magic bad" % r["set"]))
            if r["bup"] and not r["ifo"]:
                tot["bup_no_ifo"] += 1
                odd.append((p, "%s BUP without IFO" % r["set"]))
            if r["set"] != "VMG" and r["ifo"] and r["nvob"] == 0:
                tot["ifo_no_titlevob"] += 1
            if r["ifo"] and r["bup"]:
                tot["pair"] += 1
                if r["same_lba"]:
                    tot["same_lba"] += 1
                    odd.append((p, "%s IFO and BUP share an LBA" % r["set"]))
                if not r["same_len"]:
                    tot["len_differ"] += 1
                    odd.append((p, "%s IFO %d B vs BUP %d B" % (r["set"], r["ifo_len"], r["bup_len"])))
                elif not r["identical"]:
                    tot["not_identical"] += 1
                    odd.append((p, "%s IFO/BUP differ from sector %d" % (r["set"], r["first_diff_sec"])))
        if nb == 0:
            tot["isos_no_bup"] += 1
        elif nb == len(rows):
            tot["isos_all_bup"] += 1

    for k, v in tot.items():
        print("%-18s %d" % (k, v))
    if odd:
        print("\n-- anomalies (%d) --" % len(odd))
        for p, m in odd:
            print("%s: %s" % (os.path.basename(p), m))
    if a.json:
        with open(a.json, "w") as f:
            json.dump({"totals": tot, "rows": allrows}, f, indent=1)


if __name__ == "__main__":
    main()
