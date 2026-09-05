#!/usr/bin/env python3
"""
gear_hunt.py — targeted mode-22 DID hunt for the MT-15 gear signal.

The gear signal is NOT a standard OBD PID and is NOT broadcast on the diagnostic
port (ATMA = NO DATA — see docs/HANDOFF.md §5). The one route the current BLE
dongle can still win on is polling a *manufacturer* mode-22 DID. This script
polls a curated, prioritized DID list (references/mt15-mode22-targets.md) instead
of a blind 0000-FFFF sweep, and flags any payload that changes as you shift.

Three modes:
  (default)   poll the curated M22_TARGETS guess list, flag payloads that change.
  --discover  ONE slow full sweep (0000-FFFF) recording every DID that answers
              positive + its payload -> a baseline map. Do this in NEUTRAL.
  --watch MAP re-poll ONLY the DIDs in a baseline map (fast), flagging any whose
              payload differs from the neutral baseline. Shift while this runs;
              the gear DID is whichever one lights up. This is the differential
              sweep: expensive discovery once, cheap per-gear comparison after.

How to run it (STAND the bike, clutch in, then click N->1->2->3->4->5->6->N):
    uv run scripts/gear_hunt.py                       # curated guesses
    uv run scripts/gear_hunt.py --discover            # full sweep, IN NEUTRAL (~2-4h)
    uv run scripts/gear_hunt.py --watch logs/did-map-<stamp>.csv   # then shift
    uv run scripts/gear_hunt.py --header 7E0 --rx 7E8 # physical addressing

Reads the shared ELM327-over-BLE transport from ble_logger.py. Every response is
captured raw to logs/session-<UTC>.jsonl (always-capture-raw); each mode also
writes a compact CSV to logs/ for desk review.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

from bleak import BleakClient

# Reuse the proven transport + parsing rather than re-implementing it.
from ble_logger import (
    DEVICE_NAME,
    ELM327BLE,
    SessionLog,
    dump_gatt,
    expand_uuid,
    find_device,
    parse_mode22,
    run_init,
)

# --- Mode-22 targeted DID list (prioritized gear hunt) ----------------------
# Canonical rationale + full reasoning: references/mt15-mode22-targets.md. These
# are HYPOTHESES, not confirmed MT-15 DIDs. Order = run order; each row is
# (DID, note). Tier 1 anchors are checkable against known mode-01 values, so a
# match proves mode 22 works before we trust the gear candidates in Tier 2.
M22_TARGETS = [
    # Tier 0 — does mode 22 answer at all? (static ident DIDs)
    ("F190", "VIN (UDS ident) — proves mode 22 answers"),
    ("F1A0", "Yamaha ident block"),
    # Tier 1 — validation anchors (compare payload to known mode-01 values)
    ("F40C", "RPM if F4XX==OBD-PID mirror  (cross-check 010C)"),
    ("F40D", "speed  (cross-check 010D)"),
    ("F411", "throttle  (cross-check 0111)"),
    ("F405", "coolant  (cross-check 0105)"),
    ("F404", "engine load  (cross-check 0104)"),
    ("000C", "RPM low-range mirror  (cross-check 010C)"),
    ("0011", "throttle low-range mirror  (cross-check 0111)"),
    # Tier 2 — gear candidates (watch for a byte stepping N->1..6)
    ("0015", "d21 gear as hex-of-21"),
    ("0021", "d21 gear as decimal-literal"),
    ("F415", "d21 gear via F4 range, hex"),
    ("F421", "d21 gear via F4 range, decimal-literal"),
    ("0014", "sweep around hex hypothesis"),
    ("0016", "sweep around hex hypothesis"),
    ("0017", "sweep around hex hypothesis"),
    ("0018", "sweep around hex hypothesis"),
    ("0020", "sweep around decimal hypothesis"),
    ("0022", "sweep around decimal hypothesis"),
    ("0023", "sweep around decimal hypothesis"),
    ("0024", "sweep around decimal hypothesis"),
]


async def enumerate_ecus(elm: ELM327BLE, log: SessionLog, probes: list[str], timeout: float):
    """Headers-on: send probe requests and list every ECU (CAN ID) that replies.

    Functional requests are answered by multiple modules; with ATH1 each reply
    line is prefixed by the responder's CAN ID. Collecting those IDs tells us how
    many ECUs are on the bus and their addresses — so we can physically address
    ONE at a time (kills the multi-ECU interleave that scrambles the gear hunt).
    For an 11-bit ISO-TP responder at 0x7E8..0x7EF, the request header is id-8
    (7E8->7E0). Other IDs are reported for --rx filtering even if the request
    header must be found by trial.
    """
    await elm.command("ATH1")   # show CAN IDs on every line
    await elm.command("ATS1")   # spaces on for clean tokenising
    await elm.command("ATCAF1")  # let the ELM reassemble ISO-TP into clean data
    seen: dict[str, str] = {}   # can_id -> a sample reply line
    log.info("--- ECU enumeration (headers on) ---")
    for probe in probes:
        text = await elm.command(probe, timeout=max(timeout, 1.0))
        for line in text.replace("\n", "\r").split("\r"):
            parts = line.strip().split()
            if len(parts) < 2:
                continue
            cid = parts[0].upper()
            if 3 <= len(cid) <= 8 and all(c in "0123456789ABCDEF" for c in cid):
                seen.setdefault(cid, f"{probe}: {line.strip()}")
    await elm.command("ATH0")   # restore headers-off for normal polling
    if not seen:
        log.warn("no ECUs enumerated — try more probes or physical addressing")
        return seen
    log.info(f"found {len(seen)} responding CAN ID(s):")
    for cid in sorted(seen):
        hdr = ""
        try:
            n = int(cid, 16)
            if 0x7E8 <= n <= 0x7EF:
                hdr = f"  ->  --header {n - 8:03X} --rx {cid}"
        except ValueError:
            pass
        log.info(f"  {cid}{hdr}   e.g. {seen[cid]}")
    log.info("    next: re-run --s21-watch / --survey with --header <req> --rx <id> per ECU (esp. the non-ECM one = dash).")
    return seen


_FRAME_RE = re.compile(r'^\s*(\d+):\s*([0-9A-Fa-f]+)\s*$')


def reassemble_isotp(text: str) -> str:
    """Reassemble the ELM's multi-frame '0:.. 1:.. 2:..' output into one hex string.

    Long ISO-TP responses print as indexed frame lines; without stitching them
    back the byte offsets shift every poll (why the raw watch was noise). We also
    drop interleaved service-21 negatives (7F 21 NRC) from a second ECU that the
    ELM splices into the numbered sequence, so a single ECU's block aligns to a
    stable length. If there are no numbered frames, fall back to raw hex.
    """
    frames = {}
    for ln in text.replace("\n", "\r").split("\r"):
        m = _FRAME_RE.match(ln)
        if m:
            frames[int(m.group(1))] = m.group(2).upper()
    s = "".join(frames[i] for i in sorted(frames)) if frames else \
        "".join(c for c in text.upper() if c in "0123456789ABCDEF")
    return re.sub(r'7F21[0-9A-F]{2}', '', s)


def parse_service21(text: str, lid: str) -> tuple[str, str]:
    """Classify a service-21 (ReadDataByLocalIdentifier) response for LID 'XX'.

    positive -> ECU answered 61<lid><data>  (data present)
    negative -> ECU answered 7F 21 <NRC>    (service/LID not supported)
    silent   -> NO DATA / timeout / junk
    Note: with functional addressing multiple ECUs may answer; the raw text (and
    session log) keeps everything — this just reports the first positive found.
    """
    compact = "".join(ch for ch in text.upper() if ch in "0123456789ABCDEF")
    positive = f"61{lid.upper()}"
    if positive in compact:
        return "positive", compact[compact.index(positive) + len(positive):]
    if "7F21" in compact:
        i = compact.index("7F21") + 4
        return "negative", compact[i:i + 2]
    return "silent", ""


async def scan_service21(elm: ELM327BLE, log: SessionLog, out_path: Path,
                         start: int, end: int, timeout: float):
    """Single pass over service-21 local IDs; record which ones answer positive.

    Service 21 (legacy ReadDataByLocalIdentifier) is a 1-byte address space, so
    the whole thing is tiny (<=256 requests). This just discovers responders —
    the shift-correlation exercise comes after, only if anything answers.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fh = out_path.open("w", buffering=1, encoding="utf-8")
    fh.write("# service-21 (ReadDataByLocalIdentifier) responder scan\n")
    fh.write(f"# generated {datetime.now(timezone.utc).isoformat()}\n")
    fh.write("lid,payload\n")

    total = end - start + 1
    log.info(f"--- service-21 SCAN 0x{start:02X}..0x{end:02X} ({total} LIDs) ---")
    log.info(f"    rough ETA {total * timeout / 60:.1f} min. Discovering responders only.")

    hits: list[tuple[str, str]] = []
    neg = silent = 0
    try:
        for n in range(start, end + 1):
            lid = f"{n:02X}"
            text = await elm.command(f"21{lid}", timeout=timeout)
            kind, payload = parse_service21(text, lid)
            if kind == "positive":
                hits.append((lid, payload))
                fh.write(f"{lid},{payload}\n")
                log.info(f"  HIT 21{lid} -> {payload}")
            elif kind == "negative":
                neg += 1
            else:
                silent += 1
    except KeyboardInterrupt:
        pass
    finally:
        fh.close()
    log.info(f"service-21 scan done: {len(hits)} responder(s), {neg} not-supported, {silent} silent")
    if hits:
        log.info(f"    responders: {', '.join('21' + l for l, _ in hits)}")
        log.info("    next: do the gear exercise — poll these while clicking N->1->2..6.")
    else:
        log.info("    no service-21 responders — ECU is likely mode-22 only. Go the --enumerate route.")


def byte_diff(a: str, b: str) -> list[tuple[int, str, str]]:
    """Positional byte differences between two hex strings -> [(index, old, new)]."""
    ba = [a[i:i + 2] for i in range(0, len(a) - 1, 2)]
    bb = [b[i:i + 2] for i in range(0, len(b) - 1, 2)]
    diffs = []
    for i in range(min(len(ba), len(bb))):
        if ba[i] != bb[i]:
            diffs.append((i, ba[i], bb[i]))
    return diffs


async def watch_service21(elm: ELM327BLE, log: SessionLog, scan_path: Path,
                          out_path: Path, lids: list[str] | None,
                          timeout: float, interval: float):
    """Poll the responding service-21 LIDs in a loop; flag which BYTE changes.

    Gear is a single byte inside one of the bulk blocks (21 01 / 21 09 / ...), so
    we diff each response byte-by-byte against the neutral baseline and announce
    the offset that moves. Shift N->1->2..6->N slowly; the gear byte steps (watch
    for a clean 0,1,2.. or the Yamaha +0x20). Loops until Ctrl+C.
    """
    baseline = {} if lids else load_did_map(scan_path)  # reuse lid,payload loader
    if lids:
        # seed baseline from a fresh read of the requested LIDs
        for lid in lids:
            text = await elm.command(f"21{lid}", timeout=timeout)
            kind, payload = parse_service21(reassemble_isotp(text), lid)
            baseline[lid.upper()] = payload if kind == "positive" else ""
    targets = sorted(baseline)
    if not targets:
        log.warn(f"no LIDs to watch (scan {scan_path} empty?) — run --service21 first")
        return

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not out_path.exists()
    fh = out_path.open("a", buffering=1, encoding="utf-8")
    if fresh:
        fh.write("ts_iso,lid,payload,baseline\n")

    log.info(f"--- service-21 WATCH: {len(targets)} LID(s) {', '.join('21'+t for t in targets)} ---")
    log.info("    SHIFT N->1->2->3->4->5->6->N slowly. A byte offset that STEPS with the shifter is gear.")
    log.info(f"    per-sample CSV -> {out_path.resolve()}   Ctrl+C is safe.")

    announced: set[str] = set()
    loop = asyncio.get_event_loop()
    stop = asyncio.Event()
    handler_set = False
    try:
        loop.add_signal_handler(signal.SIGINT, stop.set)
        handler_set = True
    except (NotImplementedError, RuntimeError):
        pass

    try:
        while not stop.is_set():
            for lid in targets:
                if stop.is_set():
                    break
                text = await elm.command(f"21{lid}", timeout=timeout)
                kind, payload = parse_service21(reassemble_isotp(text), lid)
                if kind != "positive":
                    continue
                base = baseline[lid]
                ts = datetime.now(timezone.utc).isoformat()
                fh.write(f"{ts},{lid},{payload},{base}\n")
                for idx, old, new in byte_diff(base, payload):
                    key = f"{lid}:{idx}:{new}"
                    if key not in announced:
                        announced.add(key)
                        log.info(f"  21{lid} byte[{idx}] {old}->{new}  (baseline {old})  <== CHANGED")
            if interval:
                await asyncio.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        if handler_set:
            loop.remove_signal_handler(signal.SIGINT)
        fh.close()
    movers = sorted({k.rsplit(":", 1)[0] for k in announced})
    log.info(f"watch done: {len(movers)} (LID,byte) position(s) moved: "
             f"{', '.join(movers) if movers else '(none)'}")


async def poll_targets(elm: ELM327BLE, log: SessionLog, out_path: Path,
                       timeout: float, rounds: int, interval: float):
    """Poll M22_TARGETS in a loop, flagging changing payloads.

    Built for the standstill gear hunt: run it, then click N->1->2..6->N slowly.
    A DID whose payload byte steps with the shifter (watch for +0x20/gear, the
    Yamaha convention) is the gear channel. rounds<=0 = loop until Ctrl+C.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not out_path.exists()
    fh = out_path.open("a", buffering=1, encoding="utf-8")
    if fresh:
        fh.write("ts_iso,did,kind,payload,note\n")

    log.info(f"--- mode-22 targeted poll: {len(M22_TARGETS)} DIDs "
             f"({'until Ctrl+C' if rounds <= 0 else f'{rounds} rounds'}) ---")
    log.info("    STAND the bike, clutch in. Click N->1->2->3->4->5->6->N slowly.")
    log.info("    Watch for a payload that STEPS with the shifter (Yamaha: +0x20/gear).")
    log.info(f"    per-DID CSV -> {out_path.resolve()}   Ctrl+C is safe.")

    last: dict[str, str] = {}
    loop = asyncio.get_event_loop()
    stop = asyncio.Event()
    handler_set = False
    try:
        loop.add_signal_handler(signal.SIGINT, stop.set)
        handler_set = True
    except (NotImplementedError, RuntimeError):
        pass

    r = 0
    try:
        while not stop.is_set() and (rounds <= 0 or r < rounds):
            r += 1
            for did, note in M22_TARGETS:
                if stop.is_set():
                    break
                text = await elm.command(f"22{did}", timeout=timeout)
                kind, payload = parse_mode22(text, did)
                ts = datetime.now(timezone.utc).isoformat()
                fh.write(f"{ts},{did},{kind},{payload},{note}\n")
                if kind == "positive":
                    changed = did in last and last[did] != payload
                    flag = "  <== CHANGED" if changed else ""
                    if changed or did not in last:
                        log.info(f"  22{did} {payload:<16} {note}{flag}")
                    last[did] = payload
            if interval:
                await asyncio.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        if handler_set:
            loop.remove_signal_handler(signal.SIGINT)
        fh.close()
    answered = sorted(last)
    log.info(f"targeted poll done: {len(answered)} DID(s) answered positive: "
             f"{', '.join(answered) if answered else '(none — retry with --header 7E0 --rx 7E8)'}")


async def lock_protocol(elm: ELM327BLE, log: SessionLog):
    """Force the auto-search to resolve with a real request BEFORE any DID poll.

    Without this, the first 22xxxx requests trigger ELM 'SEARCHING...' and slow
    replies that arrive a command late — desyncing responses (the payload bleed
    seen in the first garage run). A real 0100 locks the protocol first.
    """
    await elm.command("0100")
    dpn = await elm.command("ATDPN")
    log.info(f"protocol locked (ATDPN) = {dpn}")


def load_did_map(path: Path) -> dict[str, str]:
    """Load a discover baseline CSV (did,payload) into {did: payload}."""
    m: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "," not in line or line.startswith("did,"):
            continue
        did, payload = line.split(",", 1)
        m[did.strip().upper()] = payload.strip()
    return m


async def survey_pages(elm: ELM327BLE, log: SessionLog, out_path: Path,
                       stride: int, timeout: float) -> list[int]:
    """Coarse pass: probe DIDs at a stride to find which 256-DID pages are alive.

    OEMs cluster live sensor values into contiguous blocks, so sampling each
    0xNN00 page at a few offsets reveals which pages hold data WITHOUT polling all
    65536 DIDs. Returns the sorted list of live page high-bytes; the caller then
    runs a dense --discover only on those pages. Trade-off: a page whose sole
    responder sits between stride samples is missed — lower --stride to reduce
    that risk (stride 16 = 4096 probes, 16 samples/page).
    """
    probes = list(range(0, 0x10000, stride))
    pages: dict[int, int] = {}
    log.info(f"--- mode-22 SURVEY: {len(probes)} probes @ stride {stride} "
             f"({0x100 // stride} samples/page) ---")
    log.info(f"    rough ETA {len(probes) * timeout / 60:.1f} min. Finds live pages, not every DID.")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fh = out_path.open("w", buffering=1, encoding="utf-8")
    fh.write("# mode-22 coarse survey — live pages (high byte with >=1 responder)\n")
    fh.write(f"# generated {datetime.now(timezone.utc).isoformat()}  stride={stride}\n")
    fh.write("did,payload\n")
    try:
        for i, num in enumerate(probes):
            did = f"{num:04X}"
            text = await elm.command(f"22{did}", timeout=timeout)
            kind, payload = parse_mode22(text, did)
            if kind == "positive":
                page = num >> 8
                if page not in pages:
                    log.info(f"  live page 0x{page:02X}xx  (first hit 22{did} -> {payload})")
                pages[page] = pages.get(page, 0) + 1
                fh.write(f"{did},{payload}\n")
            if i % 256 == 0 and i:
                log.info(f"  ...{i}/{len(probes)} probed, {len(pages)} live page(s) so far")
    except KeyboardInterrupt:
        pass
    finally:
        fh.close()
    live = sorted(pages)
    log.info(f"survey done: {len(live)} live page(s): "
             f"{', '.join(f'{p:02X}' for p in live) if live else '(none)'}")
    if live:
        page_arg = ",".join(f"{p:02X}" for p in live)
        est = len(live) * 0x100 * timeout / 60
        log.info(f"    next (IN NEUTRAL, ~{est:.0f} min):  "
                 f"uv run scripts/gear_hunt.py --discover --pages {page_arg}")
    return live


async def discover_dids(elm: ELM327BLE, log: SessionLog, out_path: Path,
                        targets: list[int], timeout: float, resume: bool):
    """Dense mode-22 sweep over an explicit DID list; record positives + payloads.

    Run this IN NEUTRAL to build the baseline map of implemented DIDs. `targets`
    is the ordered list of DID integers to poll — a full range, or just the pages
    that --survey found alive. Only positives are written. Resumable: the
    .progress checkpoint stores how many targets are done, so Ctrl+C / a dropped
    BLE link resumes where it left off (same target list required).
    """
    progress = out_path.with_suffix(".progress")
    seen: dict[str, str] = {}
    done = 0
    fresh = True
    if resume and out_path.exists():
        seen = load_did_map(out_path)
        fresh = False
        if progress.exists():
            done = int(progress.read_text().strip())
        log.info(f"resuming discover: skipping first {done} target(s), {len(seen)} hits so far")

    total = len(targets)
    log.info(f"--- mode-22 DISCOVER: {total - done} DIDs — do this IN NEUTRAL ---")
    log.info(f"    rough ETA {(total - done) * timeout / 60:.0f}-{(total - done) * (timeout + 0.1) / 60:.0f} min. Ctrl+C is safe (resumable).")

    f = out_path.open("a" if not fresh else "w", buffering=1, encoding="utf-8")
    if fresh:
        f.write("# mode-22 implemented-DID baseline (capture in NEUTRAL)\n")
        f.write(f"# generated {datetime.now(timezone.utc).isoformat()}\n")
        f.write("did,payload\n")

    hits = len(seen)
    try:
        for i in range(done, total):
            num = targets[i]
            did = f"{num:04X}"
            text = await elm.command(f"22{did}", timeout=timeout)
            kind, payload = parse_mode22(text, did)
            if kind == "positive":
                hits += 1
                seen[did] = payload
                f.write(f"{did},{payload}\n")
                log.info(f"  HIT 22{did} -> {payload}")
            if i % 512 == 0 and i:
                progress.write_text(str(i))
                log.info(f"  ...{i}/{total} scanned  (hits={hits})")
            done = i + 1
    except KeyboardInterrupt:
        pass
    finally:
        f.close()
        progress.write_text(str(done))
    log.info(f"discover done: {hits} implemented DID(s) -> {out_path.resolve()}")
    log.info(f"    next: shift into gear and run  --watch {out_path.name}")


def build_targets(pages: str | None, start: str, end: str) -> list[int]:
    """DID integers to sweep: explicit --pages (e.g. '00,01,F4') or a start..end range."""
    if pages:
        hi = [int(p, 16) for p in pages.split(",") if p.strip()]
        return [(p << 8) + o for p in hi for o in range(0x100)]
    return list(range(int(start, 16), int(end, 16) + 1))


async def watch_dids(elm: ELM327BLE, log: SessionLog, baseline_path: Path,
                     out_path: Path, timeout: float, interval: float):
    """Re-poll only the baseline DIDs; flag any payload that differs from neutral.

    This is the differential half of the sweep: with the baseline captured in
    neutral, anything that DIFFERS as you shift is gear-correlated. At standstill
    KOEO the other channels (temp/voltage) are stable, so a clean gear byte
    stands out. Loops until Ctrl+C.
    """
    baseline = load_did_map(baseline_path)
    dids = sorted(baseline)
    if not dids:
        log.warn(f"baseline {baseline_path} has no DIDs — run --discover first")
        return

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not out_path.exists()
    fh = out_path.open("a", buffering=1, encoding="utf-8")
    if fresh:
        fh.write("ts_iso,did,payload,baseline,differs\n")

    log.info(f"--- WATCH {len(dids)} implemented DIDs vs neutral baseline "
             f"({baseline_path.name}) ---")
    log.info("    SHIFT through the gears. A DID that DIFFERS from baseline is the gear candidate.")
    log.info(f"    per-sample CSV -> {out_path.resolve()}   Ctrl+C is safe.")

    announced: set[str] = set()
    loop = asyncio.get_event_loop()
    stop = asyncio.Event()
    handler_set = False
    try:
        loop.add_signal_handler(signal.SIGINT, stop.set)
        handler_set = True
    except (NotImplementedError, RuntimeError):
        pass

    try:
        while not stop.is_set():
            for did in dids:
                if stop.is_set():
                    break
                text = await elm.command(f"22{did}", timeout=timeout)
                kind, payload = parse_mode22(text, did)
                if kind != "positive":
                    continue
                base = baseline[did]
                differs = payload != base
                ts = datetime.now(timezone.utc).isoformat()
                fh.write(f"{ts},{did},{payload},{base},{int(differs)}\n")
                if differs:
                    # Announce a newly-differing DID, and every subsequent change.
                    key = f"{did}:{payload}"
                    if key not in announced:
                        announced.add(key)
                        log.info(f"  22{did} {payload:<16} (baseline {base})  <== DIFFERS")
            if interval:
                await asyncio.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        if handler_set:
            loop.remove_signal_handler(signal.SIGINT)
        fh.close()
    movers = sorted({k.split(":")[0] for k in announced})
    log.info(f"watch done: {len(movers)} DID(s) differed from baseline: "
             f"{', '.join(movers) if movers else '(none — was the baseline captured in neutral?)'}")


async def main():
    parser = argparse.ArgumentParser(description="Targeted mode-22 gear-signal hunt for the MT-15.")
    parser.add_argument("--name", default=DEVICE_NAME, help="BLE device name to connect to")
    parser.add_argument("--scan-timeout", type=float, default=15.0, help="seconds to scan for the dongle")
    parser.add_argument("--write-char", default="fff2",
                        help="characteristic to write commands to (default fff2; try fff1 if no responses)")
    parser.add_argument("--notify-char", default="fff1", help="characteristic to subscribe for responses")
    parser.add_argument("--timeout", type=float, default=0.3, help="per-request timeout (silent DIDs fail fast)")
    parser.add_argument("--rounds", type=int, default=0, help="poll rounds (0 = loop until Ctrl+C, the default)")
    parser.add_argument("--interval", type=float, default=0.3, help="seconds between rounds")
    parser.add_argument("--header", help="ATSH header for physical ECU addressing, e.g. 7E0 (if functional is silent)")
    parser.add_argument("--rx", help="ATCRA receive-address filter, e.g. 7E8")
    parser.add_argument("--enumerate", action="store_true",
                        help="headers-on: list every ECU (CAN ID) that replies, to find the dash module, then exit")
    parser.add_argument("--enumerate-probes", default="0100,2103,210B",
                        help="comma-separated requests to enumerate with (default 0100,2103,210B)")
    parser.add_argument("--service21", action="store_true",
                        help="scan service-21 local IDs for responders (do this first per the d-code lead), then exit")
    parser.add_argument("--s21-start", default="00", help="service-21 scan start LID hex (default 00)")
    parser.add_argument("--s21-end", default="FF", help="service-21 scan end LID hex (default FF = whole space)")
    parser.add_argument("--s21-watch", metavar="SCAN",
                        help="poll the responding LIDs from a --service21 scan CSV; flag which BYTE steps as you shift")
    parser.add_argument("--lids", help="comma-separated LIDs to watch instead of a scan file, e.g. 01,09")
    parser.add_argument("--survey", action="store_true",
                        help="coarse pass: probe at --stride to find live 256-DID pages, then exit")
    parser.add_argument("--stride", type=int, default=32,
                        help="survey probe stride (default 32 = 2048 probes, 8 samples/page; lower = more thorough)")
    parser.add_argument("--discover", action="store_true",
                        help="dense mode-22 sweep; record implemented DIDs + payloads (do IN NEUTRAL), then exit")
    parser.add_argument("--pages", help="restrict --discover to these page high-bytes, e.g. 00,01,F4 (from --survey)")
    parser.add_argument("--watch", metavar="MAP",
                        help="re-poll only the DIDs in a discover baseline CSV, flag payloads differing from it")
    parser.add_argument("--start", default="0000", help="discover range start DID hex when no --pages (default 0000)")
    parser.add_argument("--end", default="FFFF", help="discover range end DID hex when no --pages (default FFFF)")
    parser.add_argument("--resume", action="store_true", help="resume a --discover sweep from its checkpoint")
    args = parser.parse_args()

    logs_dir = Path(__file__).resolve().parent.parent / "logs"
    logs_dir.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log = SessionLog(logs_dir / f"session-{stamp}.jsonl")
    log.info(f"logging to {log.path}")

    try:
        device = await find_device(log, args.name, args.scan_timeout)
        async with BleakClient(device) as client:
            log.info(f"connected: {client.is_connected}")
            await dump_gatt(client, log)
            elm = ELM327BLE(client, log,
                            write_uuid=expand_uuid(args.write_char),
                            notify_uuid=expand_uuid(args.notify_char))
            log.info(f"write -> {elm.write_uuid}, notify <- {elm.notify_uuid}")
            await elm.start()
            await run_init(elm, log)
            await lock_protocol(elm, log)  # flush SEARCHING before any DID poll
            if args.header:
                await elm.command(f"ATSH{args.header}")
            if args.rx:
                await elm.command(f"ATCRA{args.rx}")
            await elm.command("ATST20")  # short ELM timeout so silent DIDs fail fast
            if args.enumerate:
                probes = [p.strip().upper() for p in args.enumerate_probes.split(",") if p.strip()]
                await enumerate_ecus(elm, log, probes, timeout=args.timeout)
            elif args.service21:
                await scan_service21(elm, log, logs_dir / f"s21-scan-{stamp}.csv",
                                     start=int(args.s21_start, 16), end=int(args.s21_end, 16),
                                     timeout=args.timeout)
            elif args.s21_watch or args.lids:
                lids = [l.strip().upper() for l in args.lids.split(",")] if args.lids else None
                await watch_service21(elm, log, Path(args.s21_watch) if args.s21_watch else Path(),
                                      logs_dir / f"s21-watch-{stamp}.csv", lids,
                                      timeout=args.timeout, interval=args.interval)
            elif args.survey:
                await survey_pages(elm, log, logs_dir / f"did-survey-{stamp}.csv",
                                   stride=args.stride, timeout=args.timeout)
            elif args.discover:
                targets = build_targets(args.pages, args.start, args.end)
                await discover_dids(elm, log, logs_dir / f"did-map-{stamp}.csv",
                                    targets=targets, timeout=args.timeout, resume=args.resume)
            elif args.watch:
                await watch_dids(elm, log, Path(args.watch),
                                 logs_dir / f"gear-watch-{stamp}.csv",
                                 timeout=args.timeout, interval=args.interval)
            else:
                await poll_targets(elm, log, logs_dir / f"gear-hunt-{stamp}.csv",
                                   timeout=args.timeout, rounds=args.rounds, interval=args.interval)
            log.info("done — disconnecting")
    finally:
        log.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
