#!/usr/bin/env python3
"""
ble_logger.py — first-light BLE monitor for the ELM327 dongle on the MT-15.

Goal for the garage session: prove the BLE link, confirm the protocol, discover
which PIDs the bike answers, and read live RPM — while capturing EVERYTHING raw
(every notification chunk, hex, timestamped) to a session log for desk analysis.

Philosophy: isolate one variable at a time. This script does the layers in order
  1. scan + connect (BLE link)
  2. dump the GATT table (what the dongle actually exposes)
  3. ELM init (ATZ/ATE0/...) — answered by the ELM chip, engine NOT required
  4. ATDPN / ATRV — protocol + battery voltage
  5. 01 00/20/40/60 — which standard PIDs the bike supports (bitmask decode)
  6. probe the interesting standard PIDs once each
  7. continuous poll loop ("rev sweep") on core PIDs until Ctrl+C

Everything is logged to logs/session-<UTC timestamp>.jsonl as structured records.

Run with:  uv run scripts/ble_logger.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

from bleak import BleakClient, BleakScanner

# --- BLE identifiers (captured from the dongle) -----------------------------
# 16-bit UUIDs expand into the standard Bluetooth base UUID.
DEVICE_NAME = "OBDII"
SERVICE_UUID = "0000fff0-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000fff1-0000-1000-8000-00805f9b34fb"  # dongle -> Mac (NOTIFY + CCCD 0x2902)
WRITE_UUID = "0000fff2-0000-1000-8000-00805f9b34fb"   # Mac -> dongle (WRITE, WRITE_NO_RESPONSE)
# Note: 0xFFF1 is ALSO writable on this dongle. If commands to 0xFFF2 get no
# response, retry writing to 0xFFF1 via --write-char fff1.


def expand_uuid(short: str) -> str:
    """Accept a 16-bit hex short code (e.g. 'fff2') or a full 128-bit UUID."""
    s = short.lower().removeprefix("0x")
    if len(s) == 4:
        return f"0000{s}-0000-1000-8000-00805f9b34fb"
    return s

# ELM327 line terminator, and the prompt it emits when a response is complete.
ELM_EOL = "\r"
ELM_PROMPT = ">"

# --- Standard OBD-II PIDs worth trying, with decoders -----------------------
# Each: request -> (metric name, unit, decode(list_of_data_bytes) -> value).
# These are the *standard* car PIDs; the MT-15 may only answer a subset (that's
# a finding in itself). Manufacturer PIDs (mode 22) come in a later chapter.
STANDARD_PIDS = {
    "0104": ("engine_load", "%", lambda b: round(b[0] * 100 / 255, 1)),
    "0105": ("coolant_temp", "C", lambda b: b[0] - 40),
    "010B": ("intake_map", "kPa", lambda b: b[0]),
    "010C": ("rpm", "rpm", lambda b: (b[0] * 256 + b[1]) / 4),
    "010D": ("speed", "km/h", lambda b: b[0]),
    "010F": ("intake_air_temp", "C", lambda b: b[0] - 40),
    "0111": ("throttle", "%", lambda b: round(b[0] * 100 / 255, 1)),
    "0142": ("module_voltage", "V", lambda b: round((b[0] * 256 + b[1]) / 1000, 2)),
    "0146": ("ambient_temp", "C", lambda b: b[0] - 40),
}

# Names for identifying standard mode-01 PIDs in the channels file. This is the
# broader "what is this PID" lookup; STANDARD_PIDS above is the subset we can
# actually decode/poll. Bit-encoded status PIDs (0101/0103/0113) are named here
# for identification even though we don't decode them yet.
PID_NAMES = {
    "0101": "monitor_status", "0103": "fuel_system_status", "0104": "engine_load",
    "0105": "coolant_temp", "0106": "short_fuel_trim_b1", "0107": "long_fuel_trim_b1",
    "010B": "intake_map", "010C": "rpm", "010D": "speed", "010E": "timing_advance",
    "010F": "intake_air_temp", "0111": "throttle", "0113": "o2_sensors_present",
    "0114": "o2_sensor1", "0115": "o2_sensor2", "011C": "obd_standard",
    "0121": "distance_with_mil", "0131": "distance_since_clear", "0133": "baro_pressure",
    "0142": "module_voltage", "0143": "abs_load", "0144": "commanded_afr",
    "0145": "rel_throttle", "0146": "ambient_temp", "0147": "abs_throttle_b",
    "014C": "commanded_throttle", "0151": "fuel_type",
    # Continuation flags, not data channels:
    "0120": "[bank] pids_21_40", "0140": "[bank] pids_41_60", "0160": "[bank] pids_61_80",
}

# The fast/high-value channels we poll continuously during the rev sweep.
CORE_POLL_PIDS = ["010C", "010D", "0111", "0105"]

# ELM init sequence. Headers OFF (ATH0) keeps standard-PID decoding clean for
# this basic version; flip to ATH1 later when doing CAN-ID / PID discovery.
INIT_SEQUENCE = [
    ("ATZ", "reset", 1.0),      # full reset; needs a moment to reboot
    ("ATE0", "echo off", 0.0),
    ("ATL0", "linefeeds off", 0.0),
    ("ATS0", "spaces off", 0.0),
    ("ATH0", "headers off", 0.0),
    ("ATSP0", "protocol auto", 0.0),
]


class SessionLog:
    """Writes structured JSONL records and echoes a human view to the console."""

    def __init__(self, path: Path):
        self.path = path
        self._f = path.open("a", encoding="utf-8")

    def _emit(self, rec: dict):
        rec["ts"] = datetime.now(timezone.utc).isoformat()
        self._f.write(json.dumps(rec) + "\n")
        self._f.flush()

    def info(self, msg: str):
        print(f"[i] {msg}")
        self._emit({"event": "info", "msg": msg})

    def warn(self, msg: str):
        print(f"[!] {msg}")
        self._emit({"event": "warn", "msg": msg})

    def tx(self, cmd: str):
        print(f"  -> {cmd}")
        self._emit({"event": "tx", "cmd": cmd})

    def rx_raw(self, data: bytes):
        # The truest capture: every raw notification chunk, exactly as received.
        self._emit({"event": "rx_raw", "hex": data.hex()})

    def rx(self, cmd: str, text: str, timed_out: bool = False):
        shown = text.replace("\r", "\\r")
        print(f"  <- {shown!r}{'  [TIMEOUT]' if timed_out else ''}")
        self._emit({"event": "rx", "cmd": cmd, "text": text, "timed_out": timed_out})

    def decoded(self, cmd: str, metric: str, value, unit: str, raw: str):
        print(f"     {metric} = {value} {unit}")
        self._emit({"event": "decoded", "cmd": cmd, "metric": metric,
                    "value": value, "unit": unit, "raw": raw})

    def close(self):
        self._f.close()


class ELM327BLE:
    """Minimal ELM327-over-BLE transport: send a command, read until the prompt."""

    def __init__(self, client: BleakClient, log: SessionLog,
                 write_uuid: str = WRITE_UUID, notify_uuid: str = NOTIFY_UUID):
        self.client = client
        self.log = log
        self.write_uuid = write_uuid
        self.notify_uuid = notify_uuid
        self._buffer = bytearray()
        self._complete = asyncio.Event()
        self._line_handler = None  # set during streaming (ATMA) mode
        self._line_buf = ""

    def _on_notify(self, _char, data: bytearray):
        self._buffer.extend(data)
        self.log.rx_raw(bytes(data))
        if self._line_handler is not None:
            # Streaming mode: dispatch each complete '\r'-terminated line as it arrives.
            self._line_buf += data.decode(errors="replace")
            while "\r" in self._line_buf:
                line, self._line_buf = self._line_buf.split("\r", 1)
                line = line.strip()
                if line:
                    self._line_handler(line)
        if ELM_PROMPT.encode() in self._buffer:
            self._complete.set()

    async def start(self):
        await self.client.start_notify(self.notify_uuid, self._on_notify)

    def set_line_handler(self, fn):
        """Enable streaming mode: call fn(line) for each line received (ATMA)."""
        self._line_handler = fn
        self._line_buf = ""

    async def write_raw(self, cmd: str):
        """Send a command without waiting for a prompt (ATMA never returns one)."""
        self.log.tx(cmd)
        await self.client.write_gatt_char(self.write_uuid, (cmd + ELM_EOL).encode(), response=False)

    async def stop_monitor(self):
        """Send a single byte to break ATMA streaming and return to the prompt."""
        self.log.tx("(stop stream)")
        await self.client.write_gatt_char(self.write_uuid, ELM_EOL.encode(), response=False)
        await asyncio.sleep(0.3)

    async def command(self, cmd: str, timeout: float = 6.0, settle: float = 0.0) -> str:
        """Send one command, return the cleaned text response (prompt stripped)."""
        self._buffer.clear()
        self._complete.clear()
        self.log.tx(cmd)
        # response=False -> write-without-response, what most BLE ELM clones want.
        await self.client.write_gatt_char(self.write_uuid, (cmd + ELM_EOL).encode(), response=False)
        timed_out = False
        try:
            await asyncio.wait_for(self._complete.wait(), timeout)
        except asyncio.TimeoutError:
            timed_out = True
        text = self._buffer.decode(errors="replace").replace(ELM_PROMPT, "").strip()
        self.log.rx(cmd, text, timed_out=timed_out)
        if settle:
            await asyncio.sleep(settle)
        return text


# --- response parsing helpers ----------------------------------------------
def _clean_lines(text: str) -> list[str]:
    return [ln.strip().upper() for ln in text.replace("\n", "\r").split("\r") if ln.strip()]


def extract_data_hex(text: str, request: str) -> str | None:
    """From a response, pull the data-byte hex for a given '01XX' request.

    Response mode = request mode + 0x40 (01 -> 41). We look for a line starting
    with that expected prefix and return whatever hex follows it.
    """
    mode = int(request[:2], 16) + 0x40
    prefix = f"{mode:02X}{request[2:].upper()}"
    for line in _clean_lines(text):
        compact = line.replace(" ", "")
        if compact.startswith(prefix):
            return compact[len(prefix):]
    return None


def hex_to_bytes(s: str) -> list[int]:
    s = "".join(ch for ch in s if ch in "0123456789ABCDEFabcdef")
    return [int(s[i:i + 2], 16) for i in range(0, len(s) - 1, 2)]


def decode_supported(data_hex: str, base_pid: int = 0x00) -> list[str]:
    """Decode a 01 00/20/40/60 bitmask into the supported 'XX' PIDs.

    The 32 bits of each bank map to PIDs (base_pid+1) .. (base_pid+0x20). For
    bank 0100 base_pid=0x00 -> PIDs 01..20; for 0120 base_pid=0x20 -> PIDs 21..40.
    """
    b = hex_to_bytes(data_hex)
    if len(b) < 4:
        return []
    bits = int.from_bytes(bytes(b[:4]), "big")
    supported = []
    for i in range(32):
        if bits & (1 << (31 - i)):
            supported.append(f"{base_pid + i + 1:02X}")
    return supported


# --- session phases ---------------------------------------------------------
async def find_device(log: SessionLog, name: str, timeout: float):
    log.info(f"scanning for '{name}' ({timeout:.0f}s)... make sure no phone/app holds the dongle")
    device = await BleakScanner.find_device_by_name(name, timeout=timeout)
    if device is None:
        log.warn(f"'{name}' not found. Nearby devices seen:")
        for d in await BleakScanner.discover(timeout=4.0):
            log.info(f"    {d.address}  {d.name!r}")
        raise SystemExit(f"Could not find a BLE device named {name!r}. Is the dongle powered (plugged in)?")
    log.info(f"found {name} at {device.address}")
    return device


async def dump_gatt(client: BleakClient, log: SessionLog):
    log.info("GATT table:")
    for service in client.services:
        log.info(f"  service {service.uuid}")
        for ch in service.characteristics:
            props = ",".join(ch.properties)
            log.info(f"    char {ch.uuid}  [{props}]")


async def run_init(elm: ELM327BLE, log: SessionLog):
    log.info("--- ELM init (engine not required) ---")
    for cmd, desc, settle in INIT_SEQUENCE:
        await elm.command(cmd, settle=settle)
    # Protocol + battery voltage: both answered by the ELM/vehicle immediately.
    dpn = await elm.command("ATDPN")
    log.info(f"protocol (ATDPN) = {dpn!r}")
    atrv = await elm.command("ATRV")
    log.info(f"battery (ATRV) = {atrv!r}")
    return dpn, atrv


async def scan_supported(elm: ELM327BLE, log: SessionLog) -> set[str]:
    log.info("--- supported standard PIDs (needs the bus live / engine on) ---")
    supported: set[str] = set()
    for base in ("0100", "0120", "0140", "0160"):
        base_pid = int(base[2:], 16)  # 0x00, 0x20, 0x40, 0x60
        text = await elm.command(base)
        data = extract_data_hex(text, base)
        if not data:
            log.info(f"  {base}: no valid response (bus may be quiet — is the engine on?)")
            break
        pids = decode_supported(data, base_pid=base_pid)
        full = [f"01{p}" for p in pids]
        log.info(f"  {base} -> supports: {', '.join(full) if full else '(none)'}")
        supported.update(full)
        # The last PID of each bank (0x20/0x40/0x60) is a flag: 'more PIDs follow'.
        next_flag = f"{base_pid + 0x20:02X}"
        if next_flag not in pids:
            break
    return supported


def write_channels_file(path: Path, supported: set[str], protocol: str, voltage: str):
    """Write the discovered supported PIDs to a human-readable channels file."""
    lines = [
        f"# Supported channels discovered on the MT-15",
        f"# generated {datetime.now(timezone.utc).isoformat()}",
        f"# protocol (ATDPN): {protocol}",
        f"# battery (ATRV): {voltage}",
        f"# {len(supported)} PID(s) supported",
        "",
    ]
    for pid in sorted(supported):
        name = PID_NAMES.get(pid, "(unknown / needs decode)")
        lines.append(f"{pid}\t{name}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def sniff_channels(elm: ELM327BLE, log: SessionLog, supported: set[str]) -> list[dict]:
    """Query every supported PID live and classify: value / needs decoding / silent."""
    log.info("--- sniffing each channel for live data ---")
    rows: list[dict] = []
    for pid in sorted(supported):
        if PID_NAMES.get(pid, "").startswith("[bank]"):
            continue  # continuation flag, not a real channel
        # Two attempts: a false 'silent' from a dropped clone response is misleading.
        data = None
        text = ""
        for _ in range(2):
            text = await elm.command(pid, timeout=2.0)
            data = extract_data_hex(text, pid)
            if data:
                break
        name = PID_NAMES.get(pid, "?")
        if not data:
            status, detail = "silent", ""
        elif pid in STANDARD_PIDS:
            metric, unit, decode = STANDARD_PIDS[pid]
            try:
                status, detail = "value", f"{decode(hex_to_bytes(data))} {unit}  (raw {data})"
            except (IndexError, ValueError):
                status, detail = "needs decoding", f"raw {data}"
        else:
            status, detail = "needs decoding", f"raw {data}"
        log.info(f"  {pid} {name:<20} {status:<14} {detail}")
        rows.append({"pid": pid, "name": name, "status": status, "detail": detail})
    return rows


def write_sniff_file(path: Path, rows: list[dict], protocol: str, voltage: str):
    """Write the live-sniff results to channels.txt as an aligned table."""
    lines = [
        "# Channel sniff on the MT-15",
        f"# generated {datetime.now(timezone.utc).isoformat()}",
        f"# protocol (ATDPN): {protocol}    battery (ATRV): {voltage}",
        "# status: value = known & decoded | needs decoding = data present, formula unknown | silent = no data",
        "",
        f"# {'PID':<7}{'name':<22}{'status':<16}detail",
    ]
    for r in rows:
        lines.append(f"{r['pid']:<7}{r['name']:<22}{r['status']:<16}{r['detail']}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_mode22(text: str, did: str) -> tuple[str, str]:
    """Classify a mode-22 response for DID 'XXYY'.

    Returns (kind, payload):
      positive -> ECU answered 62<did><data>   (data present)
      negative -> ECU answered 7F 22 <NRC>     (explicitly not available)
      silent   -> NO DATA / timeout / junk     (nothing there)
    """
    compact = "".join(ch for ch in text.upper() if ch in "0123456789ABCDEF")
    positive = f"62{did.upper()}"
    if positive in compact:
        return "positive", compact[compact.index(positive) + len(positive):]
    if "7F22" in compact:
        i = compact.index("7F22") + 4
        return "negative", compact[i:i + 2]  # negative response code
    return "silent", ""


async def sweep_mode22(elm: ELM327BLE, log: SessionLog, out_path: Path,
                       start: int, end: int, timeout: float, resume: bool):
    """Brute-force manufacturer mode 22 (22 XXYY) and record channels that answer.

    Only channels with a POSITIVE response are written to the file (a 62.. reply
    = data present = 'needs decoding'). Negative/NO-DATA are treated as 'silent'
    and counted, not listed — otherwise the file would be ~65k mostly-empty rows.
    Progress is checkpointed so a dropped BLE link can be resumed with --m22-resume.
    """
    progress_path = out_path.with_suffix(".progress")
    begin = start
    fresh = True
    if resume and progress_path.exists():
        begin = int(progress_path.read_text().strip(), 16) + 1
        fresh = False
        log.info(f"resuming mode-22 sweep from 0x{begin:04X}")

    total = end - begin + 1
    log.info(f"--- mode 22 sweep 0x{begin:04X}..0x{end:04X} ({total} DIDs) ---")
    log.info(f"    rough ETA {total * 0.15 / 60:.0f}-{total * 0.3 / 60:.0f} min. Ctrl+C is safe (resumable).")

    f = out_path.open("a" if not fresh else "w", encoding="utf-8")
    if fresh:
        f.write("# Mode-22 manufacturer channel sweep on the MT-15\n")
        f.write(f"# generated {datetime.now(timezone.utc).isoformat()}\n")
        f.write("# status: needs decoding = positive 62.. response (data present, formula unknown)\n")
        f.write("# (negative / NO-DATA channels are 'silent' and omitted; see session log for counts)\n\n")
        f.write(f"# {'PID':<9}{'name':<20}{'status':<16}detail\n")
        f.flush()

    hits = neg = silent = 0
    try:
        for n in range(begin, end + 1):
            did = f"{n:04X}"
            text = await elm.command(f"22{did}", timeout=timeout)
            kind, payload = parse_mode22(text, did)
            if kind == "positive":
                hits += 1
                line = f"{'22' + did:<9}{'?':<20}{'needs decoding':<16}raw {payload}"
                f.write(line + "\n")
                f.flush()
                log.info(f"  HIT 22{did} -> {payload}")
            elif kind == "negative":
                neg += 1
            else:
                silent += 1
            if n % 512 == 0:
                progress_path.write_text(did)
                log.info(f"  ...0x{did} scanned  (hits={hits} negative={neg} silent={silent})")
    finally:
        f.close()
        progress_path.write_text(f"{end:04X}")
    log.info(f"mode-22 sweep done: {hits} channels with data, {neg} unsupported, {silent} silent")


_HEX = set("0123456789ABCDEF")


def parse_can_line(line: str) -> tuple[str, list[str]] | None:
    """Parse one ATMA monitor line into (can_id, [byte_hex, ...]).

    Handles both spaced ('7E8 03 41 0C') and unspaced ('7E80341...') output, and
    filters ELM status text (SEARCHING, STOPPED, BUFFER FULL, NO DATA, '?').
    """
    parts = line.upper().split()
    if len(parts) < 2:
        return None
    cid = parts[0]
    data_hex = "".join(parts[1:])
    if not cid or any(c not in _HEX for c in cid):
        return None
    if not data_hex or len(data_hex) % 2 or any(c not in _HEX for c in data_hex):
        return None
    return cid, [data_hex[i:i + 2] for i in range(0, len(data_hex), 2)]


async def monitor_all(elm: ELM327BLE, log: SessionLog, out_path: Path,
                      duration: float, protocol: str | None):
    """Passively monitor ALL CAN frames (ATMA) and stream each to a file.

    No runtime analysis: every parsed frame is appended (line-buffered, flushed
    per line) as it arrives, so ending the script at any moment — even a hard
    kill — loses nothing. We probe the capture at the desk afterwards.
    """
    # Lock onto the real protocol with a working request BEFORE monitoring, so
    # ATMA watches the correct bus config. A forced (possibly wrong) ATSP is why
    # a monitor can see zero frames on a bus that answers polls fine.
    await elm.command(f"ATSP{protocol}" if protocol else "ATSP0")
    await elm.command("0100")            # a real request forces protocol auto-lock
    dpn = await elm.command("ATDPN")     # now reports the ACTUAL locked protocol
    log.info(f"    locked protocol (ATDPN) = {dpn}")
    # Monitor needs headers ON (to see CAN IDs) and auto-formatting OFF (raw frames).
    await elm.command("ATH1")   # show CAN IDs
    await elm.command("ATS1")   # spaces on (readability)
    await elm.command("ATCAF0")  # CAN auto-formatting off -> raw broadcast frames

    counters = {"lines": 0}
    # Line-buffered text file: each '\n' is flushed to the OS, so a mid-run stop
    # keeps every frame written so far. Format: ts_iso,can_id,data_hex
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not out_path.exists()
    fh = out_path.open("a", buffering=1, encoding="utf-8")
    if fresh:
        fh.write("ts_iso,can_id,data_hex\n")

    def on_line(line: str):
        parsed = parse_can_line(line)
        if parsed is None:
            return
        cid, data = parsed
        counters["lines"] += 1
        fh.write(f"{datetime.now(timezone.utc).isoformat()},{cid},{''.join(data)}\n")

    run_until_stop = duration <= 0
    log.info(f"--- ATMA passive monitor: {'until Ctrl+C' if run_until_stop else f'{duration:.0f}s'} ---")
    log.info(f"    streaming frames to {out_path.resolve()}")
    log.info("    SHIFT THROUGH ALL GEARS slowly (1-N-2-3-4-5-6), pause in each. Blip throttle too.")
    log.info("    press Ctrl+C when done — nothing is lost if you stop mid-run.")
    elm.set_line_handler(on_line)
    await elm.write_raw("ATMA")
    loop = asyncio.get_event_loop()
    start = loop.time()
    next_report = start + 3

    # Stop cleanly on Ctrl+C via a signal handler (robust across asyncio's SIGINT
    # quirks), so we close the file and stop the stream no matter when you stop.
    stop = asyncio.Event()
    handler_set = False
    try:
        loop.add_signal_handler(signal.SIGINT, stop.set)
        handler_set = True
    except (NotImplementedError, RuntimeError):
        pass  # fall back to KeyboardInterrupt handling below

    try:
        while not stop.is_set() and (run_until_stop or (loop.time() - start < duration)):
            await asyncio.sleep(0.4)
            if loop.time() >= next_report:
                elapsed = loop.time() - start
                rate = counters["lines"] / elapsed if elapsed else 0
                log.info(f"    {elapsed:.0f}s: {counters['lines']} frames written, {rate:.0f} fps")
                next_report += 3
    except KeyboardInterrupt:
        pass
    finally:
        if handler_set:
            loop.remove_signal_handler(signal.SIGINT)
        if stop.is_set():
            log.info("monitor stopped by user")
        await elm.stop_monitor()
        elm.set_line_handler(None)
        fh.close()

    log.info(f"captured {counters['lines']} frames -> {out_path.resolve()}")


async def read_pid(elm: ELM327BLE, req: str, timeout: float = 1.0):
    """Query one PID and return its decoded value, or None if silent/undecodable."""
    text = await elm.command(req, timeout=timeout)
    data = extract_data_hex(text, req)
    if data is None or req not in STANDARD_PIDS:
        return None
    try:
        return STANDARD_PIDS[req][2](hex_to_bytes(data))
    except (IndexError, ValueError):
        return None


async def ride_log(elm: ELM327BLE, log: SessionLog, out_path: Path, interval: float):
    """Poll RPM + speed until Ctrl+C, streaming ts,rpm,speed to a CSV.

    No runtime gear math (write raw, analyze at the desk). Each row is flushed as
    written, so stopping mid-ride — even a hard kill — loses nothing. Gear bands
    are found afterwards from the RPM/speed ratio.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not out_path.exists()
    fh = out_path.open("a", buffering=1, encoding="utf-8")
    if fresh:
        fh.write("ts_iso,rpm,speed_kmh\n")

    log.info("--- ride log: RPM + speed until Ctrl+C ---")
    log.info(f"    streaming to {out_path.resolve()}")
    log.info("    RIDE through the gears under load; spend time steady in each gear.")
    log.info("    press Ctrl+C when done — nothing is lost if you stop mid-run.")

    loop = asyncio.get_event_loop()
    stop = asyncio.Event()
    handler_set = False
    try:
        loop.add_signal_handler(signal.SIGINT, stop.set)
        handler_set = True
    except (NotImplementedError, RuntimeError):
        pass

    n = 0
    start = loop.time()
    next_report = start + 3
    try:
        while not stop.is_set():
            rpm = await read_pid(elm, "010C", timeout=1.0)
            spd = await read_pid(elm, "010D", timeout=1.0)
            ts = datetime.now(timezone.utc).isoformat()
            fh.write(f"{ts},{'' if rpm is None else rpm},{'' if spd is None else spd}\n")
            n += 1
            if loop.time() >= next_report:
                log.info(f"    {loop.time() - start:.0f}s: {n} samples "
                         f"(last rpm={rpm} speed={spd})")
                next_report += 3
            if interval:
                await asyncio.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        if handler_set:
            loop.remove_signal_handler(signal.SIGINT)
        fh.close()
    log.info(f"logged {n} samples -> {out_path.resolve()}")


async def probe_standard(elm: ELM327BLE, log: SessionLog):
    log.info("--- probing interesting standard PIDs once each ---")
    for req, (metric, unit, decode) in STANDARD_PIDS.items():
        text = await elm.command(req)
        data = extract_data_hex(text, req)
        if data is None:
            continue
        try:
            value = decode(hex_to_bytes(data))
            log.decoded(req, metric, value, unit, data)
        except (IndexError, ValueError):
            log.warn(f"  {req} ({metric}): could not decode data {data!r}")


async def poll_loop(elm: ELM327BLE, log: SessionLog, interval: float, duration: float,
                    pids: list[str] | None = None):
    pids = pids or CORE_POLL_PIDS
    log.info(f"--- rev sweep: polling {pids} every {interval}s for {duration:.0f}s ---")
    log.info("    blip the throttle and record the tacho on your phone for later correlation. Ctrl+C to stop.")
    loop = asyncio.get_event_loop()
    end = loop.time() + duration
    try:
        while loop.time() < end:
            for req in pids:
                text = await elm.command(req, timeout=2.0)
                data = extract_data_hex(text, req)
                if data is None:
                    continue
                metric, unit, decode = STANDARD_PIDS[req]
                try:
                    log.decoded(req, metric, decode(hex_to_bytes(data)), unit, data)
                except (IndexError, ValueError):
                    pass
            await asyncio.sleep(interval)
    except KeyboardInterrupt:
        log.info("poll loop stopped by user")


async def main():
    parser = argparse.ArgumentParser(description="First-light BLE monitor for the ELM327 / MT-15.")
    parser.add_argument("--name", default=DEVICE_NAME, help="BLE device name to connect to")
    parser.add_argument("--scan-timeout", type=float, default=15.0, help="seconds to scan for the dongle")
    parser.add_argument("--interval", type=float, default=0.2, help="seconds between poll cycles")
    parser.add_argument("--duration", type=float, default=90.0, help="rev-sweep duration in seconds")
    parser.add_argument("--no-poll", action="store_true", help="skip the continuous poll loop")
    parser.add_argument("--write-char", default="fff2",
                        help="characteristic to write commands to (default fff2; try fff1 if no responses)")
    parser.add_argument("--notify-char", default="fff1", help="characteristic to subscribe for responses")
    parser.add_argument("--pids", help="comma-separated PIDs to poll, e.g. 010C,010D (overrides defaults)")
    parser.add_argument("--rpm-only", action="store_true",
                        help="skip discovery/probe, stream only RPM (010C)")
    parser.add_argument("--scan-channels", action="store_true",
                        help="discover supported PIDs, write them to --channels-file, then exit")
    parser.add_argument("--sniff", action="store_true",
                        help="discover PIDs, query each live (value/needs decoding/silent), write channels.txt, exit")
    parser.add_argument("--sweep-mode22", action="store_true",
                        help="brute-force manufacturer mode 22 (22 XXYY), write channels_mode22.txt, exit")
    parser.add_argument("--m22-start", default="0000", help="mode-22 sweep start DID hex (default 0000)")
    parser.add_argument("--m22-end", default="00FF",
                        help="mode-22 sweep end DID hex (default 00FF = quick validation; use FFFF for full 65536)")
    parser.add_argument("--m22-timeout", type=float, default=0.3, help="per-request timeout for the sweep")
    parser.add_argument("--m22-header", help="ATSH header for physical ECU addressing, e.g. 7E0 (if functional is silent)")
    parser.add_argument("--m22-rx", help="ATCRA receive-address filter, e.g. 7E8")
    parser.add_argument("--m22-resume", action="store_true", help="resume a sweep from the last checkpoint")
    parser.add_argument("--monitor", action="store_true",
                        help="ATMA passive CAN monitor: capture all frames, flag gear candidates, exit")
    parser.add_argument("--monitor-duration", type=float, default=0.0,
                        help="monitor capture seconds (0 = run until Ctrl+C, the default)")
    parser.add_argument("--monitor-protocol", default="",
                        help="force ATSP protocol (blank=auto-lock via a real request, recommended; 6/7/8/9 to force)")
    parser.add_argument("--ride", action="store_true",
                        help="log RPM+speed to a CSV until Ctrl+C (for gear-from-ratio analysis at the desk)")
    parser.add_argument("--ride-interval", type=float, default=0.1, help="seconds between ride-log samples")
    parser.add_argument("--channels-file", default=None,
                        help="where to write discovered channels (default <project root>/channels.txt)")
    args = parser.parse_args()

    poll_pids = None
    if args.rpm_only:
        poll_pids = ["010C"]
    elif args.pids:
        poll_pids = [p.strip().upper() for p in args.pids.split(",") if p.strip()]

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
            protocol, voltage = await run_init(elm, log)
            default_channels = Path(__file__).resolve().parent.parent / "channels.txt"
            out_path = Path(args.channels_file) if args.channels_file else default_channels
            if args.ride:
                await ride_log(elm, log, logs_dir / f"ride-{stamp}.csv", args.ride_interval)
            elif args.monitor:
                mon_path = logs_dir / f"canbus-{stamp}.csv"
                await monitor_all(elm, log, mon_path, args.monitor_duration, args.monitor_protocol)
            elif args.sweep_mode22:
                if args.m22_header:
                    await elm.command(f"ATSH{args.m22_header}")
                if args.m22_rx:
                    await elm.command(f"ATCRA{args.m22_rx}")
                await elm.command("ATST20")  # shorter ELM timeout so silent DIDs fail fast
                m22_path = Path(__file__).resolve().parent.parent / "channels_mode22.txt"
                await sweep_mode22(elm, log, m22_path,
                                   start=int(args.m22_start, 16), end=int(args.m22_end, 16),
                                   timeout=args.m22_timeout, resume=args.m22_resume)
            elif args.sniff:
                supported = await scan_supported(elm, log)
                rows = await sniff_channels(elm, log, supported)
                write_sniff_file(out_path, rows, protocol, voltage)
                live = sum(1 for r in rows if r["status"] != "silent")
                log.info(f"sniffed {len(rows)} channels ({live} carrying data) -> {out_path.resolve()}")
            elif args.scan_channels:
                supported = await scan_supported(elm, log)
                write_channels_file(out_path, supported, protocol, voltage)
                log.info(f"wrote {len(supported)} channel(s) to {out_path.resolve()}")
            else:
                if not args.rpm_only:
                    await scan_supported(elm, log)
                    await probe_standard(elm, log)
                if not args.no_poll:
                    await poll_loop(elm, log, args.interval, args.duration, pids=poll_pids)
            log.info("done — disconnecting")
    finally:
        log.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
