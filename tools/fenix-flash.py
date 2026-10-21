#!/usr/bin/env python3
"""fenix-flash: interactive eMMC partition flasher for the HY300 Lite over the U-Boot serial console."""

import argparse
import atexit
import glob
import hashlib
import itertools
import json
import os
import pwd
import random
import re
import select
import shutil
import struct
import sys
import termios
import time
import tty
import zlib
from collections import deque
from pathlib import Path

import serial
from serial.tools import list_ports

# settings
APP_NAME = "fenix-flash"
APP_VERSION = "1.0.0"
TARGET = "HY300 Lite (fenix)"
LOG_FILE = "fenix-flash.log"
STATE_FILE = "fenix-flash-%s.state"

BAUD = 1_500_000
PROMPT = "=>"
DEVICE_GLOBS = ("/dev/ttyACM*", "/dev/ttyUSB*")
MMC_DEV = 0
RAM_ADDR = 0x10000000
VERIFY_RAM_ADDR = 0x14000000
CRITICAL_PARTITIONS = ("security", "uboot", "trust")
SPARSE_MAGIC = b"\x3a\xff\x26\xed"
MOUNT_SKIP = ("/dev/loop", "/dev/ram", "/dev/zram")

MIB = 1024 * 1024
SECTOR = 512
ROW = 16
WINDOW_BYTES = 4 * MIB
RESUME_CHECK_BYTES = 1 * MIB
RECOVERY_STEP = 1 * MIB
BACKTRACK_LIMIT = 16 * MIB

BLOCK_LEVELS = (512, 1024, 2048, 4096, 8192, 16384, 32768, 49152)
TARGET_LOSS = 0.004
SWITCH_MARGIN = 0.05
EVAL_BYTES = 256 * 1024
COOLDOWN_BYTES = 4 * MIB
EWMA_ALPHA = 0.3

FAIL_RETRIES = 3
LINE_RETRIES = 8
WRITE_RETRIES = 3
REPAIR_ROUNDS = 6
REPAIR_RUN_ROWS = 32
REPAIR_BATCH = 6
PART_READ_TRIES = 6
LINE_LEVELS = (700, 400, 200, 100)
SELFTEST_BYTES = 8 * 1024
TX_CHUNK = 8
TX_DELAY = 0.0003
TX_PROFILES = ((8, 0.0003), (16, 0.0003), (32, 0.0003), (64, 0.0003), (128, 0.0003))
PACE_UP_LINES = 40
PACE_HOLD_LINES = 400
ECHO_IDLE = 0.3

EST_RATE = 4 * 1024
LONG_FLASH_SECONDS = 2 * 3600
SPEED_WINDOW_S = 15
UI_REFRESH = 0.25
POPUP_SECONDS = 5

ROOT_NOTICE = ("fenix-flash is recommended to run as root (su or sudo).",
               "Root can read any image file and always has access to the USB-TTL adapter.",
               "Without it, folders and files you cannot read will be refused.")

MD_LINE = re.compile(r"^([0-9a-fA-F]{8,16}): ((?:[0-9a-fA-F]{8} ){3}[0-9a-fA-F]{8})( +.*)$")
CRC_LINE = re.compile(r"==>\s*([0-9a-fA-F]{8})[ \t]*$", re.M)
PART_LINE = re.compile(r'^\s*(\d+)\s+0x([0-9a-fA-F]+)\s+0x([0-9a-fA-F]+)\s+"([^"]*)"\s*$')


class TransferError(Exception):
    pass


class CRCMismatch(TransferError):
    pass


class ConsoleTimeout(TransferError):
    pass


class VerifyMismatch(TransferError):
    pass


class FatalError(RuntimeError):
    pass


# helpers
def log(message):
    try:
        with open(LOG_FILE, "a") as handle:
            handle.write("%s %s\n" % (time.strftime("%m/%d/%Y %H:%M:%S"), message))
    except OSError:
        pass


def fmt_size(value):
    value = float(value)
    for unit in ("B", "KiB", "MiB"):
        if value < 1024:
            return "%d B" % value if unit == "B" else "%.1f %s" % (value, unit)
        value /= 1024
    return "%.2f GiB" % value


def fmt_time(seconds):
    seconds = int(seconds)
    return "%d:%02d:%02d" % (seconds // 3600, seconds % 3600 // 60, seconds % 60)


def ascii_col(raw):
    return "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in raw)


def padded_size(size):
    return -(-size // SECTOR) * SECTOR


def is_root():
    return os.geteuid() == 0


def parse_rows(text, base, length):
    rows, bad = {}, set()
    end = base + length
    for line in text.split("\n"):
        match = MD_LINE.match(line)
        if not match:
            continue
        addr = int(match.group(1), 16)
        if not base <= addr < end or (addr - base) % ROW or addr in bad:
            continue
        raw = b"".join(int(w, 16).to_bytes(4, "little") for w in match.group(2).split())
        tail = match.group(3)
        if not (tail.endswith(ascii_col(raw)) and not tail[:-ROW].strip(" ")):
            bad.add(addr)
            continue
        previous = rows.get(addr)
        if previous is not None and previous != raw:
            bad.add(addr)
            del rows[addr]
            continue
        rows[addr] = raw
    return rows


def group_runs(addrs, max_rows):
    runs = []
    for addr in addrs:
        if runs and addr == runs[-1][0] + runs[-1][1] * ROW and runs[-1][1] < max_rows:
            runs[-1][1] += 1
        else:
            runs.append([addr, 1])
    return runs


def build_commands(addr, data):
    """Turn a data block into U-Boot mw.l commands; runs of equal words become one fill."""
    words = struct.unpack("<%dI" % (len(data) // 4), data)
    commands, pos = [], 0
    for value, group in itertools.groupby(words):
        count = sum(1 for _ in group)
        at = addr + pos * 4
        if count > 1:
            commands.append("mw.l %x %x %x" % (at, value, count))
        else:
            commands.append("mw.l %x %x" % (at, value))
        pos += count
    return commands


def pack_lines(commands, limit):
    lines, current, size = [], [], 0
    for command in commands:
        extra = len(command) + 2
        if current and size + extra > limit:
            lines.append("; ".join(current))
            current, size = [], 0
        current.append(command)
        size += extra
    if current:
        lines.append("; ".join(current))
    return lines


class Partition:
    def __init__(self, number, start, end, name):
        self.number, self.start, self.end, self.name = number, start, end, name

    @property
    def sectors(self):
        return self.end - self.start + 1

    @property
    def size(self):
        return self.sectors * SECTOR


def state_path(part):
    return Path(STATE_FILE % part.name)


def load_state(path, part, size, sha):
    try:
        meta = json.loads(path.read_text())
        offset = int(meta["offset"])
        same = (meta["version"] == 1 and meta["partition"] == part.name
                and meta["start"] == part.start and meta["sectors"] == part.sectors
                and meta["size"] == size and meta["sha256"] == sha)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not same or offset < 0 or offset > padded_size(size) or offset % SECTOR:
        return None
    return offset


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(MIB)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def validate_image(image, part):
    if not image.is_file():
        raise FatalError("Image not found: %s" % image)
    size = image.stat().st_size
    if size == 0:
        raise FatalError("The image file is empty.")
    with image.open("rb") as handle:
        if handle.read(4) == SPARSE_MAGIC:
            raise FatalError("%s is an Android sparse image.\n"
                             "Convert it to a raw image first (for example with simg2img)." % image.name)
    if padded_size(size) > part.size:
        raise FatalError("The image (%s) is larger than partition '%s' (%s)." %
                         (fmt_size(size), part.name, fmt_size(part.size)))
    return size


# adaptive block size
class AdaptiveBlock:
    def __init__(self):
        self.level = len(BLOCK_LEVELS) - 1
        self.tp = {}
        self.loss = {}
        self.cool = {}
        self.since_eval = 0

    @property
    def size(self):
        return BLOCK_LEVELS[self.level]

    def _ewma(self, store, key, value):
        old = store.get(key)
        store[key] = value if old is None else old + EWMA_ALPHA * (value - old)

    def update(self, nbytes, rows, lost, seconds, failed):
        for key in self.cool:
            self.cool[key] = max(0, self.cool[key] - nbytes)
        current = self.level
        if failed:
            self.tp.pop(current, None)
            self.cool[current] = COOLDOWN_BYTES
            self.level = max(0, current - 1)
            self.since_eval = 0
            return
        self._ewma(self.tp, current, nbytes / max(seconds, 1e-3))
        self._ewma(self.loss, current, lost / max(rows, 1))
        self.since_eval += nbytes
        if self.since_eval >= EVAL_BYTES:
            self.since_eval = 0
            self.level = self._decide(current)

    def _decide(self, current):
        best = max(self.tp, key=self.tp.get)
        if best != current and self.tp[best] > self.tp[current] * (1 + SWITCH_MARGIN):
            return best
        if self.loss[current] > TARGET_LOSS:
            scaled = BLOCK_LEVELS[current] * TARGET_LOSS / self.loss[current]
            candidate = max((i for i, b in enumerate(BLOCK_LEVELS) if b <= scaled), default=0)
            if candidate < current and candidate not in self.tp:
                return candidate
            return current
        upper = current + 1
        if upper < len(BLOCK_LEVELS) and upper not in self.tp and not self.cool.get(upper):
            return upper
        return current


# serial console
class Console:
    def __init__(self, port, baud):
        self.baud = baud
        self.ser = serial.Serial(port, baud, timeout=0.05, write_timeout=5,
                                 xonxoff=False, rtscts=False, dsrdtr=False, exclusive=True)
        self.prompt = re.compile(rb"(?:^|\n)" + re.escape(PROMPT.encode()) + rb"[ \t]*$")
        self.session = "%04x" % random.randrange(1 << 16)
        self.seq = 0
        self.resyncs = 0
        self.idle = None
        self.level = 0
        self.clean = 0
        self.hold = {}

    def close(self):
        try:
            self.ser.write(b"\x03")
            self.ser.flush()
        except Exception:
            pass
        try:
            self.ser.close()
        except Exception:
            pass

    def _send(self, data, paced=False):
        chunk, delay = TX_PROFILES[self.level] if paced else (TX_CHUNK, TX_DELAY)
        for i in range(0, len(data), chunk):
            self.ser.write(data[i:i + chunk])
            self.ser.flush()
            time.sleep(delay)

    def pace_ok(self):
        for key in self.hold:
            self.hold[key] = max(0, self.hold[key] - 1)
        self.clean += 1
        upper = self.level + 1
        if self.clean >= PACE_UP_LINES and upper < len(TX_PROFILES) and not self.hold.get(upper):
            self.level = upper
            self.clean = 0

    def pace_fail(self):
        self.hold[self.level] = PACE_HOLD_LINES
        self.level = max(0, self.level - 1)
        self.clean = 0

    def drain(self, quiet=0.05, limit=3.0):
        last = time.monotonic()
        end = last + limit
        while True:
            piece = self.ser.read(self.ser.in_waiting or 1)
            now = time.monotonic()
            if piece:
                last = now
            if now - last >= quiet or now >= end:
                return

    def resync(self, initial=False):
        if not initial:
            self.resyncs += 1
        for _ in range(6):
            try:
                self.ser.write(b"\x03")
                self.ser.flush()
                self.drain(quiet=0.3, limit=6.0)
                self.run("echo resync", timeout=3.0, recover=False)
                return
            except ConsoleTimeout:
                continue
        raise FatalError("The U-Boot console is not responding.\n"
                         "Stop the projector at the U-Boot prompt (%s) and check the port." % PROMPT)

    def confirm_echo(self, line):
        """Read back what U-Boot echoed; the line is only executed when it matches exactly."""
        want = line.encode("ascii")
        buf = bytearray()
        last = time.monotonic()
        ok = False
        while True:
            piece = self.ser.read(self.ser.in_waiting or 1)
            now = time.monotonic()
            if piece:
                buf += piece
                last = now
                typed = bytes(buf).lstrip()
                if len(typed) >= len(want):
                    ok = typed == want
                    break
            elif now - last > ECHO_IDLE:
                break
        if ok:
            self.pace_ok()
            return
        self.pace_fail()
        self.ser.write(b"\x03")
        self.ser.flush()
        self.drain(quiet=0.1, limit=2.0)
        raise TransferError("command echo mismatch before execution")

    def run(self, cmd, timeout=30.0, check_echo=False, recover=True, verify_first=False):
        self.seq += 1
        tag = "SYNC%s_%d" % (self.session, self.seq)
        line = "%s; echo %s" % (cmd, tag)
        marker = ("\n" + tag).encode()
        self.ser.reset_input_buffer()
        if verify_first:
            self._send(line.encode("ascii"), paced=True)
            self.confirm_echo(line)
            self.ser.write(b"\r")
            self.ser.flush()
        else:
            self._send(line.encode("ascii") + b"\r")

        buf = bytearray()
        cut = None
        deadline = time.monotonic() + timeout
        while True:
            if time.monotonic() > deadline:
                if recover:
                    self.resync()
                raise ConsoleTimeout("timeout waiting for: %s" % cmd[:60])
            piece = self.ser.read(min(self.ser.in_waiting or 1, 65536))
            if not piece:
                if self.idle:
                    self.idle()
                continue
            scan_from = max(0, len(buf) - len(marker))
            buf += piece
            if cut is None:
                found = buf.find(marker, scan_from)
                if found >= 0:
                    cut = found
            if cut is not None and self.prompt.search(buf, cut):
                break

        text = bytes(buf[:cut]).decode("latin-1").replace("\r", "")
        if check_echo and not verify_first and line not in text:
            raise TransferError("command echo mismatch")
        return text


# transfer engine
class Engine:
    def __init__(self, con, prog, ram, vram):
        self.con, self.prog, self.ram, self.vram = con, prog, ram, vram
        self.block = AdaptiveBlock()
        self.line_max = LINE_LEVELS[0]
        self.rate = EST_RATE
        self.lost = 0
        self.retries = 0
        self.recovery = None
        self.part = None
        self.image = None
        self.img = None
        self.sha = ""
        self.size = 0
        self.total = 0
        self.offset = 0
        self.statef = None

    def md_timeout(self, length):
        return 10.0 + (length / ROW) * 70 * 10 / self.con.baud * 6

    def run_checked(self, cmd, timeout, ok, verify_first=False):
        last = ""
        for attempt in range(1, 4):
            try:
                text = self.con.run(cmd, timeout, check_echo=True, verify_first=verify_first)
            except TransferError as exc:
                last = str(exc)
                self.prog.event("%s (attempt %d/3)" % (last, attempt))
                continue
            if ok(text):
                return text
            last = text.strip()[-200:]
            self.prog.event("unexpected reply to '%s' (attempt %d/3)" % (cmd, attempt))
            time.sleep(0.2)
        raise FatalError("Command failed: %s\n%s" % (cmd, last))

    def crc(self, addr, length, timeout=180.0):
        text = self.con.run("crc32 0x%x 0x%x" % (addr, length), timeout)
        match = CRC_LINE.search(text)
        if not match:
            raise TransferError("unrecognized crc32 reply")
        return int(match.group(1), 16)

    def stable_crc(self, addr, length):
        previous = None
        for _ in range(6):
            try:
                current = self.crc(addr, length)
            except TransferError:
                continue
            if current == previous:
                return current
            previous = current
        raise FatalError("Could not obtain a stable CRC32 from U-Boot.")

    def select_mmc(self):
        self.run_checked("mmc dev %d" % MMC_DEV, 15,
                         lambda t: "current device" in t or "mmc%d" % MMC_DEV in t)

    def read_partitions(self):
        previous = None
        for _ in range(PART_READ_TRIES):
            try:
                text = self.con.run("mmc part", 20)
            except TransferError:
                continue
            found = []
            for line in text.split("\n"):
                match = PART_LINE.match(line)
                if match:
                    found.append((int(match.group(1)), int(match.group(2), 16),
                                  int(match.group(3), 16), match.group(4)))
            valid = (bool(found) and [p[0] for p in found] == list(range(1, len(found) + 1))
                     and all(p[2] >= p[1] for p in found))
            if valid and found == previous:
                return [Partition(*p) for p in found]
            previous = found if valid else None
        raise FatalError("Could not read a consistent GPT partition table with 'mmc part'.")

    def probe_write(self, part):
        text = self.run_checked("mmc write 0x%x 0x%x 0x0" % (self.ram, part.start), 15,
                                lambda t: True, verify_first=True)
        if re.search(r"unknown command|usage:|unknown subcommand", text, re.I):
            raise FatalError("This U-Boot does not provide a working 'mmc write' command.")

    def selftest(self):
        data = os.urandom(SELFTEST_BYTES)
        expected = zlib.crc32(data) & 0xFFFFFFFF
        for level in LINE_LEVELS:
            self.line_max = level
            began = time.monotonic()
            try:
                self.push_data(self.ram, data)
                ok = self.stable_crc(self.ram, len(data)) == expected
            except TransferError:
                ok = False
            if ok:
                self.rate = len(data) / max(time.monotonic() - began, 1e-3)
                log("selftest ok: line limit %d chars, %.0f B/s" % (level, self.rate))
                return
            log("selftest failed with a line limit of %d chars" % level)
        raise FatalError("The U-Boot console could not receive data reliably.\n"
                         "Check the adapter, the baud rate and the cabling.")

    def read_chunk(self, addr, length):
        cmd = "md.l 0x%x 0x%x; crc32 0x%x 0x%x" % (addr, length // 4, addr, length)
        text = self.con.run(cmd, self.md_timeout(length))
        rows = parse_rows(text, addr, length)
        match = CRC_LINE.search(text)
        expected = int(match.group(1), 16) if match else None

        wanted = range(addr, addr + length, ROW)
        missing = [a for a in wanted if a not in rows]

        # repair missing or corrupted rows
        for _ in range(REPAIR_ROUNDS):
            if not missing:
                break
            runs = group_runs(missing, REPAIR_RUN_ROWS)
            for i in range(0, len(runs), REPAIR_BATCH):
                batch = runs[i:i + REPAIR_BATCH]
                batch_cmd = "; ".join("md.l 0x%x 0x%x" % (s, c * 4) for s, c in batch)
                reply = self.con.run(batch_cmd, self.md_timeout(sum(c for _, c in batch) * ROW))
                for a, r in parse_rows(reply, addr, length).items():
                    rows.setdefault(a, r)
            missing = [a for a in wanted if a not in rows]
        if missing:
            raise TransferError("%d rows could not be recovered" % len(missing))

        data = b"".join(rows[a] for a in wanted)
        if expected is None:
            expected = self.crc(addr, length, 30.0)
        actual = zlib.crc32(data) & 0xFFFFFFFF
        if actual != expected:
            raise CRCMismatch("block CRC mismatch (host %08x, U-Boot %08x)" % (actual, expected))
        return data, 0

    def push_data(self, addr, data):
        lines = pack_lines(build_commands(addr, data), self.line_max)
        retried = 0
        for line in lines:
            for attempt in range(1, LINE_RETRIES + 1):
                try:
                    self.con.run(line, 4.0, verify_first=True)
                    break
                except TransferError as exc:
                    retried += 1
                    self.prog.event("line retry %d/%d at 0x%x: %s" % (attempt, LINE_RETRIES, addr, exc))
            else:
                raise TransferError("a command line could not be delivered")
        return len(lines), retried

    def stage_once(self, addr, data):
        lines, retried = self.push_data(addr, data)
        expected = zlib.crc32(data) & 0xFFFFFFFF
        if self.stable_crc(addr, len(data)) == expected:
            return lines, retried

        # read the block back and rewrite only the rows that differ
        current, _ = self.read_chunk(addr, len(data))
        bad = [a for a in range(0, len(data), ROW) if current[a:a + ROW] != data[a:a + ROW]]
        self.prog.event("block CRC mismatch at 0x%x: rewriting %d rows" % (addr, len(bad)))
        if len(bad) * ROW > len(data) // 2:
            raise CRCMismatch("block at 0x%x does not match after writing" % addr)
        for start, count in group_runs([addr + a for a in bad], REPAIR_RUN_ROWS):
            offset = start - addr
            more, again = self.push_data(start, data[offset:offset + count * ROW])
            lines += more
            retried += again + 1
        if self.stable_crc(addr, len(data)) != expected:
            raise CRCMismatch("block at 0x%x does not match after repair" % addr)
        return lines, retried

    def stage(self, addr, data):
        failed, last = False, None
        for attempt in range(1, FAIL_RETRIES + 1):
            try:
                lines, retried = self.stage_once(addr, data)
                return lines, retried, failed
            except TransferError as exc:
                failed, last = True, exc
                self.retries += 1
                self.prog.event("retry %d/%d at 0x%x: %s" % (attempt, FAIL_RETRIES, addr, exc))
                time.sleep(0.05 * attempt)
        half = (len(data) // 2 // ROW) * ROW
        if half < ROW or half >= len(data):
            raise FatalError("Block at 0x%x could not be staged in RAM: %s" % (addr, last))
        left = self.stage(addr, data[:half])
        right = self.stage(addr + half, data[half:])
        return left[0] + right[0], left[1] + right[1], True

    def stage_window(self, window, start):
        pos = 0
        while pos < len(window):
            size = min(self.block.size, len(window) - pos)
            began = time.monotonic()
            lines, retried, failed = self.stage(self.ram + pos, window[pos:pos + size])
            elapsed = time.monotonic() - began
            # Clear stale retry/error text after a block is staged successfully.
            if self.prog.event_text != "-":
                self.prog.event_text = "-"
            pos += size
            self.lost += retried
            self.block.update(size, max(lines, 1), retried, elapsed, failed)
            self.prog.update(start + pos, self.block.size, self.lost, self.retries, self.con.resyncs)

    def write_sectors(self, lba, sectors):
        if lba < self.part.start or lba + sectors > self.part.start + self.part.sectors:
            raise FatalError("Refusing to write outside the selected partition.")
        pattern = re.compile(r"\b%d\s+blocks written:\s*OK" % sectors)
        self.run_checked("mmc write 0x%x 0x%x 0x%x" % (self.ram, lba, sectors), 300,
                         lambda t: bool(pattern.search(t)), verify_first=True)

    def read_back_crc(self, lba, sectors, length):
        pattern = re.compile(r"\b%d\s+blocks read:\s*OK" % sectors)
        self.run_checked("mmc read 0x%x 0x%x 0x%x" % (self.vram, lba, sectors), 300,
                         lambda t: bool(pattern.search(t)))
        return self.stable_crc(self.vram, length)

    def read_image(self, offset, length):
        self.img.seek(offset)
        data = self.img.read(length)
        if len(data) < length:
            data += bytes(length - len(data))
        return data

    def device_matches(self, offset, length):
        expected = zlib.crc32(self.read_image(offset, length)) & 0xFFFFFFFF
        lba = self.part.start + offset // SECTOR
        return self.read_back_crc(lba, length // SECTOR, length) == expected

    def verify_resume_point(self):
        overlap = min(self.offset, RESUME_CHECK_BYTES)
        if not self.device_matches(self.offset - overlap, overlap):
            raise FatalError("The device does not match the image at the resume point.\n"
                             "Start over to flash this partition again.")

    def recover_write_failure(self, error):
        failure = self.offset
        step = RECOVERY_STEP
        recovery = self.recovery

        if recovery and recovery["anchor"] <= failure <= recovery["failure"]:
            failure = recovery["failure"]
            candidate = recovery["anchor"]
        else:
            self.recovery = {"failure": failure, "anchor": failure}
            candidate = failure - step

        self.prog.event("Write verification failed near 0x%x: %s" % (failure, error))
        self.prog.status("Verification failed: checking earlier data (limit 16 MiB)")
        checked = {}

        def valid(offset):
            if offset in checked:
                return checked[offset]
            try:
                checked[offset] = self.device_matches(offset, step)
            except TransferError as exc:
                checked[offset] = False
                self.prog.event("Recovery: block 0x%x could not be checked: %s" % (offset, exc))
            else:
                self.prog.event("Recovery: block 0x%x %s" %
                                (offset, "matches the image" if checked[offset] else "differs from the image"))
            return checked[offset]

        resume = None
        while candidate >= step:
            previous = candidate - step
            if failure - previous > BACKTRACK_LIMIT:
                break
            if valid(candidate) and valid(previous):
                resume = previous
                break
            candidate -= step

        if resume is None:
            raise FatalError(
                "Write verification failed and no two consecutive valid blocks were found "
                "within 16 MiB before partition offset 0x%x.\n"
                "The eMMC may be failing or write-protected. The partition is partially written." % failure)

        self.recovery.update({"failure": failure, "anchor": resume})
        self.prog.event("Recovery: rewinding from 0x%x to 0x%x" % (self.offset, resume))
        self.offset = resume
        self.checkpoint()
        self.prog.rebase(resume)

    def save_state(self):
        tmp = Path(str(self.statef) + ".tmp")
        with tmp.open("w") as handle:
            json.dump({"version": 1, "partition": self.part.name, "start": self.part.start,
                       "sectors": self.part.sectors, "size": self.size, "sha256": self.sha,
                       "image": str(self.image), "offset": self.offset}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.statef)

    def checkpoint(self):
        self.save_state()

    def flash_window(self):
        start = self.offset
        length = min(WINDOW_BYTES, self.total - start)
        windows = -(-self.total // WINDOW_BYTES)
        label = "Window %d/%d" % (start // WINDOW_BYTES + 1, windows)
        window = self.read_image(start, length)
        reference = zlib.crc32(window) & 0xFFFFFFFF
        lba = self.part.start + start // SECTOR
        sectors = length // SECTOR

        staged = False
        for attempt in range(1, WRITE_RETRIES + 1):
            # stage the window in RAM and check it against the image
            if staged and self.stable_crc(self.ram, length) != reference:
                self.prog.event("%s: RAM content changed, staging again" % label)
                staged = False
            if not staged:
                if attempt > 1:
                    self.prog.rebase(start)
                self.prog.status("%s: staging into RAM" % label)
                self.stage_window(window, start)
                self.prog.status("%s: checking the staged data" % label)
                if self.stable_crc(self.ram, length) != reference:
                    self.retries += 1
                    self.prog.event("%s: staged data CRC mismatch (attempt %d/%d)" %
                                    (label, attempt, WRITE_RETRIES))
                    continue
                staged = True

            # write to the eMMC and read it back
            self.prog.status("%s: writing to the eMMC" % label)
            self.write_sectors(lba, sectors)
            self.prog.status("%s: verifying the written data" % label)
            if self.read_back_crc(lba, sectors, length) == reference:
                self.offset = start + length
                self.checkpoint()
                self.prog.update(self.offset, self.block.size, self.lost, self.retries, self.con.resyncs)
                return
            self.retries += 1
            self.prog.event("%s: eMMC verification failed (attempt %d/%d)" % (label, attempt, WRITE_RETRIES))
        raise VerifyMismatch("window at 0x%x could not be verified after writing" % start)

    def final_verify(self):
        windows = -(-self.total // WINDOW_BYTES)
        pos = 0
        while pos < self.total:
            length = min(WINDOW_BYTES, self.total - pos)
            self.prog.status("Final verification: window %d/%d" % (pos // WINDOW_BYTES + 1, windows))
            if not self.device_matches(pos, length):
                raise FatalError("Final verification failed at partition offset 0x%x." % pos)
            pos += length

    def flash(self, part, image, offset, sha):
        self.part, self.image, self.sha = part, image, sha
        self.size = image.stat().st_size
        self.total = padded_size(self.size)
        self.statef = state_path(part)
        self.offset = offset
        self.recovery = None
        self.con.idle = self.prog.tick

        with image.open("rb") as handle:
            self.img = handle
            try:
                if offset:
                    self.prog.status("Verifying the resume point")
                    self.verify_resume_point()
                self.checkpoint()
                while self.offset < self.total:
                    try:
                        self.flash_window()
                    except VerifyMismatch as exc:
                        self.recover_write_failure(exc)
                    else:
                        if self.recovery and self.offset > self.recovery["failure"]:
                            self.recovery = None
                self.final_verify()
            finally:
                self.img = None
                try:
                    self.checkpoint()
                except (OSError, ValueError):
                    pass

        self.statef.unlink(missing_ok=True)
        self.con.idle = None


# terminal ui
def read_key(timeout=None):
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        if timeout is not None and not select.select([fd], [], [], timeout)[0]:
            return None
        key = os.read(fd, 1)
        if key == b"\x1b":
            while len(key) < 8 and select.select([fd], [], [], 0.05)[0]:
                key += os.read(fd, 1)
        return key
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


class UI:
    def __init__(self):
        self.tty = sys.stdin.isatty() and sys.stdout.isatty()
        self.color = self.tty and "NO_COLOR" not in os.environ
        atexit.register(self.show_cursor)

    def paint(self, text, *codes):
        return "\x1b[%sm%s\x1b[0m" % (";".join(codes), text) if self.color else text

    def cols(self):
        return shutil.get_terminal_size((80, 24)).columns

    def hide_cursor(self):
        if self.tty:
            sys.stdout.write("\x1b[?25l")
            sys.stdout.flush()

    def show_cursor(self):
        if self.tty:
            sys.stdout.write("\x1b[?25h")
            sys.stdout.flush()

    def banner(self):
        if self.tty:
            sys.stdout.write("\x1b[2J\x1b[H")
        print(self.paint("  %s %s" % (APP_NAME, APP_VERSION), "1", "38;5;208"))
        print(self.paint("  eMMC partition flasher for the %s over U-Boot serial" % TARGET, "2"))
        print(self.paint("  " + "─" * max(10, min(60, self.cols() - 4)), "2"))
        print()

    def heading(self, title, note=None):
        print("  " + self.paint(title, "1"))
        for line in (note.split("\n") if note else []):
            print("  " + self.paint(line, "2"))
        print()

    def wait_enter(self):
        if not self.tty:
            input()
            return
        print(self.paint("  Press Enter to continue", "2"))
        while read_key() not in (b"\r", b"\n", b"q", b"\x1b"):
            pass

    def message(self, title, lines, error=False):
        self.show_cursor()
        self.banner()
        print("  " + self.paint(title, "1", "31" if error else "32"))
        print()
        for line in lines:
            print("  " + line)
        print()
        self.wait_enter()

    def notice(self, title, lines, seconds=POPUP_SECONDS):
        self.show_cursor()
        self.banner()
        print("  " + self.paint(title, "1", "33"))
        print()
        for line in lines:
            print("  " + line)
        print()
        if self.tty:
            print(self.paint("  Continuing in %d seconds (press any key to skip)" % seconds, "2"))
            read_key(seconds)

    def type_confirm(self, title, lines, expected):
        self.show_cursor()
        self.banner()
        print("  " + self.paint(title, "1", "31"))
        print()
        for line in lines:
            print("  " + line)
        print()
        try:
            return input("  Type '%s' to continue: " % expected).strip() == expected
        except EOFError:
            return False

    def task(self, label, func):
        sys.stdout.write("  %s ... " % label)
        sys.stdout.flush()
        try:
            result = func()
        except BaseException:
            print(self.paint("failed", "31"))
            raise
        print(self.paint("ok", "32"))
        return result

    def menu(self, title, options, default=0, note=None):
        self.banner()
        self.heading(title, note)
        count = len(options)
        if count == 0:
            return None

        default = min(max(default, 0), count - 1)

        if not self.tty:
            for i, option in enumerate(options, 1):
                print("  %d) %s" % (i, option))
            while True:
                answer = input("> ").strip().lower()
                if answer == "":
                    return default
                if answer == "q":
                    return None
                if answer.isdigit() and 1 <= int(answer) <= count:
                    return int(answer) - 1

        rows = shutil.get_terminal_size((80, 24)).lines
        view = min(count, max(3, rows - 11 - (note.count("\n") + 1 if note else 0)))
        index, first = default, True
        self.hide_cursor()
        try:
            while True:
                top = max(0, min(index - view // 2, count - view))
                if not first:
                    sys.stdout.write("\x1b[%dA" % (view + 2))
                first = False
                out = []
                for i in range(top, top + view):
                    text = ("  > " if i == index else "   ") + options[i]
                    text = text[:self.cols() - 1]
                    out.append("\x1b[2K\r" + (self.paint(text, "7") if i == index else text))
                out.append("\x1b[2K\r")
                out.append("\x1b[2K\r" + self.paint("  up/down: move  enter: select  q: quit", "2"))
                sys.stdout.write("\n".join(out) + "\n")
                sys.stdout.flush()
                key = read_key()
                if key in (b"\x1b[A", b"k"):
                    index = (index - 1) % count
                elif key in (b"\x1b[B", b"j"):
                    index = (index + 1) % count
                elif key in (b"\r", b"\n"):
                    return index
                elif key in (b"q", b"\x1b"):
                    return None
        finally:
            self.show_cursor()

    def confirm(self, question, details=()):
        choice = self.menu("%s" % question, ["Yes", "No"], default=1, note="\n".join(details) or None)
        return choice == 0


def invoking_home():
    name = os.environ.get("SUDO_USER")
    if name and is_root():
        try:
            return pwd.getpwnam(name).pw_dir
        except KeyError:
            pass
    return str(Path.home())


def list_mounts():
    mounts, seen = [], set()

    def add(path):
        try:
            real = os.path.realpath(path)
            usage = shutil.disk_usage(real)
        except OSError:
            return
        if real in seen or not os.path.isdir(real):
            return
        seen.add(real)
        mounts.append((Path(real), real, usage.total, usage.free))

    add(invoking_home())
    add("/")
    try:
        with open("/proc/mounts") as handle:
            lines = handle.read().splitlines()
    except OSError:
        lines = []
    for line in lines:
        fields = line.split()
        if len(fields) < 3 or not fields[0].startswith("/dev/") or fields[0].startswith(MOUNT_SKIP):
            continue
        add(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), fields[1]))
    return mounts


def list_dir(path, show_hidden):
    dirs, files = [], []
    with os.scandir(path) as entries:
        for entry in entries:
            if not show_hidden and entry.name.startswith("."):
                continue
            try:
                if entry.is_dir():
                    dirs.append((entry.name, 0))
                elif entry.is_file():
                    files.append((entry.name, entry.stat().st_size))
            except OSError:
                continue
    order = lambda item: item[0].casefold()
    return sorted(dirs, key=order), sorted(files, key=order)


def row_text(item, cols):
    name, kind, size = item
    if kind == "up":
        return "../"
    if kind == "dir":
        return name + "/"
    room = max(8, cols - 20)
    shown = name if len(name) <= room else name[:room - 1] + "…"
    return "%s  %10s" % (shown.ljust(room), fmt_size(size))


class Browser:
    """Full-screen file picker: mount points in the top-right corner, folders in the middle."""

    def __init__(self, ui, start=None):
        self.ui = ui
        self.mounts = list_mounts()
        self.mount = self.pick_mount(start)
        self.root = self.mounts[self.mount][0]
        self.cwd = self.root
        self.items = []
        self.cursor = 0
        self.mcursor = self.mount
        self.mode = "files"
        self.hidden = False
        self.popup = None
        self.popup_until = 0.0
        self.chosen = None
        self.finished = False
        self.start = start

    def pick_mount(self, start):
        best = 0
        if start is not None:
            size = -1
            for i, mount in enumerate(self.mounts):
                root = mount[0]
                if (start == root or root in start.parents) and len(str(root)) > size:
                    best, size = i, len(str(root))
        return best

    def load(self, path, root):
        dirs, files = list_dir(path, self.hidden)
        items = [("..", "up", 0)] if path != root else []
        items += [(name, "dir", 0) for name, _ in dirs]
        items += [(name, "file", size) for name, size in files]
        return items

    def alert(self, title, detail):
        self.popup = (title, detail)
        self.popup_until = time.monotonic() + POPUP_SECONDS

    def enter(self, path, focus=None, root=None):
        root = root or self.root
        try:
            items = self.load(path, root)
        except OSError as exc:
            self.alert("Cannot open %s" % path, exc.strerror or "read error")
            return False
        self.root, self.cwd, self.items = root, path, items
        self.cursor = 0
        for i, item in enumerate(items):
            if focus and item[0] == focus:
                self.cursor = i
        return True

    def draw(self):
        paint = self.ui.paint
        size = shutil.get_terminal_size((80, 24))
        cols, rows = size.columns, size.lines
        view = max(3, rows - 7)

        mount = self.mounts[self.mount]
        right = "^ %s (%s)" % (mount[1], fmt_size(mount[2]))
        path = str(self.cwd)
        room = max(8, cols - len(right) - 6)
        if len(path) > room:
            path = "…" + path[-(room - 1):]
        pad = max(1, cols - 3 - len(path) - len(right))
        lines = [
            paint("  %s %s" % (APP_NAME, APP_VERSION), "1", "38;5;208"),
            paint("  Select the image to flash", "2"),
            paint("  " + "─" * max(10, min(60, cols - 4)), "2"),
            "  " + path + " " * pad + (paint(right, "7") if self.mode == "mounts" else paint(right, "1")),
        ]

        if self.mode == "mounts":
            texts = ["%-30s %10s   (%s free)" % (m[1][:30], fmt_size(m[2]), fmt_size(m[3])) for m in self.mounts]
            index = self.mcursor
        else:
            texts = [row_text(item, cols) for item in self.items]
            index = self.cursor
        body = []
        if not texts:
            body.append(paint("  (empty)", "2"))
        else:
            top = max(0, min(index - view // 2, len(texts) - view))
            for i in range(top, min(len(texts), top + view)):
                text = ("  " + texts[i] + (" <" if i == index else ""))[:cols - 1]
                body.append(paint(text, "7") if i == index else text)
        body += [""] * (view - len(body))

        if self.popup and view >= 6:
            title, detail = self.popup
            width = min(cols - 6, max(len(title), len(detail), 20) + 4)
            margin = " " * max(0, (cols - width) // 2)
            box = ["┌" + "─" * (width - 2) + "┐",
                   "│ " + title[:width - 4].ljust(width - 4) + " │",
                   "│ " + detail[:width - 4].ljust(width - 4) + " │",
                   "└" + "─" * (width - 2) + "┘"]
            body[-4:] = [margin + paint(line, "1", "31") for line in box]

        hint = ("  up/down: move   enter: open/select   left: parent   h: hidden   m: mount points   q: back"
                if self.mode == "files" else "  up/down: choose   enter: select the mount point   esc: cancel")
        lines += body + ["", paint(hint[:cols - 1], "2")]
        sys.stdout.write("\x1b[H" + "\n".join("\x1b[2K" + line for line in lines) + "\x1b[J")
        sys.stdout.flush()

    def handle_mounts(self, key):
        count = len(self.mounts)
        if key in (b"\x1b[A", b"k"):
            self.mcursor = (self.mcursor - 1) % count
        elif key in (b"\x1b[B", b"j"):
            self.mcursor = (self.mcursor + 1) % count
        elif key in (b"\r", b"\n"):
            root = self.mounts[self.mcursor][0]
            if self.enter(root, root=root):
                self.mount = self.mcursor
                self.mode = "files"
        elif key in (b"\x1b", b"q", b"\x1b[D", b"m", b"\t"):
            self.mode = "files"

    def handle_files(self, key):
        count = len(self.items)
        page = max(3, shutil.get_terminal_size((80, 24)).lines - 8)
        if key in (b"\x1b[A", b"k"):
            if self.cursor == 0:
                self.mcursor = self.mount
                self.mode = "mounts"
            else:
                self.cursor -= 1
        elif key in (b"\x1b[B", b"j"):
            self.cursor = min(self.cursor + 1, max(0, count - 1))
        elif key == b"\x1b[5~":
            self.cursor = max(0, self.cursor - page)
        elif key == b"\x1b[6~":
            self.cursor = min(max(0, count - 1), self.cursor + page)
        elif key in (b"\x1b[H", b"\x1b[1~"):
            self.cursor = 0
        elif key in (b"\x1b[F", b"\x1b[4~"):
            self.cursor = max(0, count - 1)
        elif key in (b"\x1b[D", b"\x7f", b"\x08"):
            if self.cwd != self.root:
                self.enter(self.cwd.parent, focus=self.cwd.name)
        elif key in (b"\r", b"\n", b"\x1b[C") and count:
            name, kind, _ = self.items[self.cursor]
            if kind == "up":
                self.enter(self.cwd.parent, focus=self.cwd.name)
            elif kind == "dir":
                self.enter(self.cwd / name)
            elif key != b"\x1b[C":
                path = self.cwd / name
                if os.access(path, os.R_OK):
                    self.chosen, self.finished = path, True
                else:
                    self.alert("Permission denied", "Cannot read %s" % path)
        elif key == b"h":
            current = self.items[self.cursor][0] if count else None
            self.hidden = not self.hidden
            self.enter(self.cwd, focus=current)
        elif key in (b"m", b"\t"):
            self.mcursor = self.mount
            self.mode = "mounts"
        elif key in (b"q", b"\x1b"):
            self.finished = True

    def run(self):
        ui = self.ui
        ui.hide_cursor()
        sys.stdout.write("\x1b[2J")
        try:
            if not self.enter(self.cwd):
                self.items = []
            if self.start is not None and self.start != self.cwd:
                self.enter(self.start)
            while not self.finished:
                self.draw()
                timeout = max(0.0, self.popup_until - time.monotonic()) if self.popup else None
                key = read_key(timeout)
                self.popup = None
                if key is None:
                    continue
                if self.mode == "mounts":
                    self.handle_mounts(key)
                else:
                    self.handle_files(key)
        finally:
            ui.show_cursor()
        return self.chosen


def browse_for_file(ui, start=None):
    if not ui.tty:
        answer = input("Image path: ").strip()
        return Path(answer).expanduser().resolve() if answer else None
    return Browser(ui, start).run()


class Progress:
    def __init__(self, ui, compact=False):
        self.ui = ui
        self.compact = compact
        self.active = False
        self.status_text = ""
        self.event_text = "-"
        self.last_draw = 0.0
        self.last_plain = 0.0
        self.panel_lines = 0
        self.done = self.block = self.lost = self.retries = self.resyncs = 0
        self.start_done = 0
        self.total = 1
        self.part = None
        self.image = None
        self.t0 = time.monotonic()
        self.hist = deque()

    def begin(self, part, image, offset, total):
        self.part, self.image = part, image
        self.total = total
        self.done = self.start_done = offset
        self.t0 = time.monotonic()
        self.hist = deque([(self.t0, offset)])
        self.active = True
        self.panel_lines = 0
        self.last_plain = 0.0
        if not self.compact:
            self.ui.banner()
            self.ui.hide_cursor()
        self.draw(force=True)

    def end(self):
        self.active = False
        if self.compact:
            if self.ui.tty:
                sys.stdout.write("\n")
                sys.stdout.flush()
        else:
            self.ui.show_cursor()

    def rebase(self, done):
        self.done = done
        self.hist = deque([(time.monotonic(), done)])
        self.draw(force=True)

    def update(self, done, block, lost, retries, resyncs):
        self.done, self.block, self.lost = done, block, lost
        self.retries, self.resyncs = retries, resyncs
        self.hist.append((time.monotonic(), done))
        self.draw()

    def status(self, text):
        log(text)
        self.status_text = text
        self.draw(force=True)

    def event(self, text):
        log(text)
        self.event_text = text
        self.draw(force=True)

    def tick(self):
        self.draw()

    def metrics(self, now):
        while len(self.hist) > 1 and now - self.hist[0][0] > SPEED_WINDOW_S:
            self.hist.popleft()
        began, base = self.hist[0]
        speed = (self.done - base) / (now - began) if now - began > 0.5 else 0.0
        speed = max(speed, 0.0)
        remaining = (self.total - self.done) / speed if speed > 0 else None
        return speed, remaining

    def render(self, now):
        cols, paint = self.ui.cols(), self.ui.paint
        speed, remaining = self.metrics(now)
        percent = 100.0 * self.done / self.total
        width = max(10, min(50, cols - 14))
        filled = int(width * self.done / self.total)
        bar = "  [" + paint("█" * filled, "32") + "░" * (width - filled) + "] " + paint("%5.1f%%" % percent, "1")

        def row(label, value):
            text = ("  " + label.ljust(11) + value)[:cols - 1]
            return paint(text[:13], "2") + text[13:]

        return [
            row("Partition", "%s (#%d)   LBA 0x%X-0x%X" % (self.part.name, self.part.number, self.part.start, self.part.end)),
            row("Image", str(self.image)),
            "",
            bar,
            "",
            row("Written", "%s / %s" % (fmt_size(self.done), fmt_size(self.total))),
            row("Speed", "%s/s" % fmt_size(speed)),
            row("Remaining", fmt_time(remaining) if remaining is not None else "--:--:--"),
            row("Elapsed", fmt_time(now - self.t0)),
            row("Block size", fmt_size(self.block) if self.block else "-"),
            row("Repairs", "%d lines   (retries %d, resyncs %d)" % (self.lost, self.retries, self.resyncs)),
            "",
            row("Status", self.status_text),
            row("Last event", self.event_text),
        ]

    def draw(self, force=False):
        if not self.active:
            return
        now = time.monotonic()
        if self.compact:
            if self.ui.tty:
                if not force and now - self.last_draw < UI_REFRESH:
                    return
                self.last_draw = now
                speed, remaining = self.metrics(now)
                eta = fmt_time(remaining) if remaining is not None else "--:--:--"
                detail = self.event_text if self.event_text != "-" else self.status_text
                line = "%5.1f%% %s/%s %s/s ETA %s blk %s r:%d | %s" % (
                    100.0 * self.done / self.total, fmt_size(self.done), fmt_size(self.total),
                    fmt_size(speed), eta, fmt_size(self.block) if self.block else "-",
                    self.retries, detail)
                width = max(1, self.ui.cols() - 1)
                sys.stdout.write("\r" + line[:width].ljust(width))
                sys.stdout.flush()
            elif force or now - self.last_plain >= 5:
                self.last_plain = now
                print("%5.1f%%  %s/%s  %s/s  %s" % (
                    100.0 * self.done / self.total, fmt_size(self.done), fmt_size(self.total),
                    fmt_size(self.metrics(now)[0]), self.status_text or self.event_text))
            return
        if not self.ui.tty:
            if force or now - self.last_plain >= 5:
                self.last_plain = now
                print("%5.1f%%  %s  %s" % (100.0 * self.done / self.total, fmt_size(self.done), self.status_text))
            return
        if not force and now - self.last_draw < UI_REFRESH:
            return
        self.last_draw = now
        lines = self.render(now)
        out = ["\x1b[%dA" % self.panel_lines] if self.panel_lines else []
        out += ["\x1b[2K\r" + line + "\n" for line in lines]
        sys.stdout.write("".join(out))
        sys.stdout.flush()
        self.panel_lines = len(lines)


# application
def find_devices():
    paths = sorted({p for pattern in DEVICE_GLOBS for p in glob.glob(pattern)})
    info = {p.device: p for p in list_ports.comports()}
    return [(p, (info[p].description if p in info else "serial device")) for p in paths]


def prepare_state(ui, part, size, sha):
    statef = state_path(part)
    if not statef.exists():
        return 0
    offset = load_state(statef, part, size, sha)
    if offset:
        choice = ui.menu("A partial flash of '%s' was found" % part.name,
                         ["Resume from %s (%.1f%%)" % (fmt_size(offset), 100.0 * offset / padded_size(size)),
                          "Start over", "Cancel"])
        if choice is None or choice == 2:
            return None
        if choice == 0:
            return offset
    elif offset is None and not ui.confirm("Leftover state from another image was found. Discard it?"):
        return None
    statef.unlink(missing_ok=True)
    return 0


def print_partition_table(parts):
    """Print partition information obtained from the connected device."""
    print("Available eMMC partitions (%d):" % len(parts))
    print("%-4s %-20s %12s %12s %12s  %s" % ("NUM", "NAME", "SIZE", "START LBA", "END LBA", "NOTE"))
    print("-" * 78)
    for part in parts:
        print("%-4d %-20s %12s 0x%08X 0x%08X  %s" %
              (part.number, part.name, fmt_size(part.size), part.start, part.end,
               "critical" if part.name in CRITICAL_PARTITIONS else ""))


def resolve_partition(parts, token):
    token = token.strip()
    if token.isdigit():
        part = next((p for p in parts if p.number == int(token)), None)
    else:
        part = next((p for p in parts if p.name.casefold() == token.casefold()), None)
    if part is None:
        available = ["%d: %s (%s)" % (p.number, p.name, fmt_size(p.size)) for p in parts]
        raise FatalError("Partition not found: %s\nAvailable partitions:\n%s" % (token, "\n".join(available)))
    return part


def batch_summary_lines(plan, completed, reason=None):
    done = {part.number for part in completed}
    missing = [(part, image) for part, image, _ in plan if part.number not in done]
    lines = []
    if reason:
        lines += ["Reason: %s" % reason, ""]
    lines.append("Flashed successfully in this run (%d):" % len(completed))
    lines += ["  %d: %s" % (p.number, p.name) for p in completed] or ["  (none)"]
    lines += ["", "Not completed (%d):" % len(missing)]
    if missing:
        lines += ["  %d: %s  <-  %s" % (p.number, p.name, image) for p, image in missing]
        lines.append("Partial progress is saved; run the same command again to resume.")
    else:
        lines.append("  (none)")
    return lines


def session(ui, engine, prog):
    con = engine.con
    try:
        ui.task("Syncing with the U-Boot console", lambda: con.resync(initial=True))
        ui.task("Selecting eMMC device %d" % MMC_DEV, engine.select_mmc)
        parts = ui.task("Reading the partition table", engine.read_partitions)
        ui.task("Checking the 'mmc write' command", lambda: engine.probe_write(parts[0]))
        ui.task("Testing the console data path", engine.selftest)
    except (FatalError, TransferError) as exc:
        ui.message("Setup failed", str(exc).split("\n"), error=True)
        return 1

    time.sleep(0.4)
    start = None
    while True:
        image = browse_for_file(ui, start)
        if image is None:
            return 0
        start = image.parent

        try:
            image_size = image.stat().st_size
        except OSError as exc:
            ui.message("Cannot read the image", [str(exc)], error=True)
            continue
        options = ["%2d  %-14s %10s   LBA 0x%08X-0x%08X%s" %
                   (p.number, p.name, fmt_size(p.size), p.start, p.end,
                    "   [critical]" if p.name in CRITICAL_PARTITIONS else "") for p in parts]
        options.append("Back to the file browser")
        choice = ui.menu("Select the partition to flash", options,
                         note="Image: %s (%s)" % (image.name, fmt_size(image_size)))
        if choice is None or choice == len(parts):
            continue
        part = parts[choice]

        try:
            size = validate_image(image, part)
        except (FatalError, OSError) as exc:
            ui.message("Invalid image", str(exc).split("\n"), error=True)
            continue

        total = padded_size(size)
        estimate = total / engine.rate
        details = ["Image     : %s" % image,
                   "Image size: %s" % fmt_size(size),
                   "Partition : %s (#%d, %s)" % (part.name, part.number, fmt_size(part.size)),
                   "Estimated : up to ~%s" % fmt_time(estimate)]
        if total < part.size:
            details.append("Note      : the image is smaller than the partition; the rest stays untouched.")
        if estimate > LONG_FLASH_SECONDS:
            details.append("Warning   : this will take a long time over serial.")
        if not ui.confirm("Flash '%s'?" % part.name, details):
            continue
        if part.name in CRITICAL_PARTITIONS and not ui.type_confirm(
                "WARNING: '%s' is a boot-critical partition" % part.name,
                ["A bad write here can leave the projector unable to boot.",
                 "The RockUSB mode is the only way back if that happens.",
                 "Flash only an image you trust and that is meant for this device."], part.name):
            continue

        try:
            ui.banner()
            sha = ui.task("Hashing the image", lambda: sha256_file(image))
            offset = prepare_state(ui, part, size, sha)
        except OSError as exc:
            ui.message("Cannot read the image", [str(exc)], error=True)
            continue
        if offset is None:
            continue

        prog.begin(part, image, offset, total)
        try:
            engine.flash(part, image, offset, sha)
        except KeyboardInterrupt:
            prog.end()
            ui.message("Flash interrupted", [
                "Progress was saved up to the last verified window.",
                "The partition is only partially written until the flash finishes.",
                "Run %s again with the same image to resume." % APP_NAME], error=True)
            return 130
        except (FatalError, TransferError, OSError) as exc:
            prog.end()
            ui.message("Flash failed", str(exc).split("\n") + [
                "", "Progress was saved up to the last verified window.",
                "The partition is only partially written until the flash finishes.",
                "Run %s again with the same image to resume." % APP_NAME], error=True)
            return 1
        prog.end()
        elapsed = time.monotonic() - prog.t0
        log("Flash complete: %s -> %s sha256=%s" % (image, part.name, sha))
        ui.message("Flash complete", [
            "Image     : %s" % image,
            "Partition : %s (#%d)" % (part.name, part.number),
            "Written   : %s" % fmt_size(total),
            "SHA-256   : %s" % sha,
            "Time      : %s (avg %s/s)" % (fmt_time(elapsed), fmt_size((total - offset) / max(elapsed, 1e-3))),
            "Repairs   : %d lines, %d retries" % (engine.lost, engine.retries),
            "",
            "Every window was read back from the eMMC and matched the image."])


def app(ui, args):
    if not is_root():
        ui.notice("Running without root privileges", ROOT_NOTICE)

    devices = find_devices()
    if not devices:
        ui.message("No serial device found",
                   ["Nothing matches %s." % ", ".join(DEVICE_GLOBS), "Check the USB-TTL bridge and try again."], error=True)
        return 1
    choice = ui.menu("Select the serial device", ["%-16s %s" % d for d in devices],
                     note="Pick the USB-TTL bridge connected to the projector UART.")
    if choice is None:
        return 0
    device = devices[choice][0]
    if not os.access(device, os.R_OK | os.W_OK):
        ui.message("Permission denied", ["Cannot access %s." % device,
                                         "Run as root, or add your user to the group that owns it (uucp or dialout) and log in again."], error=True)
        return 1
    if not ui.confirm("Use %s at %d baud?" % (device, args.baud),
                      ["Stop the projector at the U-Boot prompt (%s) before continuing." % PROMPT]):
        return 0

    ui.banner()
    prog = Progress(ui)
    try:
        con = ui.task("Opening %s" % device, lambda: Console(device, args.baud))
    except serial.SerialException as exc:
        ui.message("Could not open the serial port", [str(exc)], error=True)
        return 1
    try:
        return session(ui, Engine(con, prog, args.ram, args.verify_ram), prog)
    finally:
        con.close()


def command_app(args, ui):
    con = None
    prog = Progress(ui, compact=True)
    plan = []
    completed = []
    try:
        if not is_root():
            print("Warning: %s" % " ".join(ROOT_NOTICE), file=sys.stderr)
        if not os.path.exists(args.adapter):
            raise FatalError("Serial adapter does not exist: %s" % args.adapter)
        if not os.access(args.adapter, os.R_OK | os.W_OK):
            raise FatalError("Cannot access %s. Run as root or check the device permissions." % args.adapter)

        print("%s %s - command-only mode" % (APP_NAME, APP_VERSION))
        print("Opening %s at %d baud..." % (args.adapter, args.baud))
        con = Console(args.adapter, args.baud)
        engine = Engine(con, prog, args.ram, args.verify_ram)

        print("Synchronizing with U-Boot...")
        con.resync(initial=True)
        print("Selecting eMMC device %d..." % MMC_DEV)
        engine.select_mmc()
        print("Reading partition table...")
        parts = engine.read_partitions()

        if args.list_partitions:
            print_partition_table(parts)
            return 0

        seen = set()
        for token, name in zip(args.partitions, args.files):
            part = resolve_partition(parts, token)
            if part.number in seen:
                raise FatalError("Partition selected more than once: %s" % token)
            seen.add(part.number)
            image = Path(name).expanduser().resolve()
            plan.append((part, image, validate_image(image, part)))

        critical = [part.name for part, _, _ in plan if part.name in CRITICAL_PARTITIONS]
        if critical and not args.allow_critical:
            raise FatalError("Boot-critical partition(s) selected: %s\n"
                             "A bad write can leave the projector unable to boot. Use --allow-critical to proceed." %
                             ", ".join(critical))

        print("\nFlash plan:")
        for part, image, size in plan:
            print("  %2d  %-16s <- %s (%s)" % (part.number, part.name, image, fmt_size(size)))
        if not args.yes:
            if not sys.stdin.isatty():
                raise FatalError("Refusing to write without confirmation. Use --yes for non-interactive runs.")
            if input("Type 'flash' to start writing to the eMMC: ").strip().lower() != "flash":
                print("Cancelled; nothing was written.")
                return 0

        print("Checking the 'mmc write' command...")
        engine.probe_write(parts[0])
        print("Testing the console data path...")
        engine.selftest()
        print("Measured %s/s on random data." % fmt_size(engine.rate))

        for part, image, size in plan:
            statef = state_path(part)
            sha = sha256_file(image)
            total = padded_size(size)
            if args.restart:
                statef.unlink(missing_ok=True)
                Path(str(statef) + ".tmp").unlink(missing_ok=True)
                offset = 0
            elif statef.exists():
                offset = load_state(statef, part, size, sha)
                if offset is None:
                    raise FatalError("Invalid or mismatching flash state found for '%s'. "
                                     "Use --restart to start over." % part.name)
                if offset:
                    print("Resuming %s at %s (%.1f%%)..." %
                          (part.name, fmt_size(offset), 100.0 * offset / total))
            else:
                offset = 0

            estimate = (total - offset) / engine.rate
            print("\nPartition: %s (#%d)" % (part.name, part.number))
            print("Image: %s | Size: %s | Estimated: up to ~%s" % (image, fmt_size(size), fmt_time(estimate)))
            print("Starting flash... Press Ctrl+C to stop; progress is checkpointed.")

            prog.begin(part, image, offset, total)
            engine.flash(part, image, offset, sha)
            prog.end()
            elapsed = time.monotonic() - prog.t0
            completed.append(part)
            log("Flash complete: %s -> %s sha256=%s" % (image, part.name, sha))
            print("Flash complete")
            print("SHA-256: %s" % sha)
            print("Time: %s (avg %s/s)" % (fmt_time(elapsed), fmt_size((total - offset) / max(elapsed, 1e-3))))
            print("Repairs: %d lines | Retries: %d | Resyncs: %d" % (engine.lost, engine.retries, con.resyncs))

        if len(plan) > 1:
            print("\n".join(["", "=== Flash summary ==="] +
                             batch_summary_lines(plan, completed, "Selected sequence finished.")))
        return 0
    except KeyboardInterrupt:
        if prog.active:
            prog.end()
        print("\nInterrupted. Progress was saved up to the last verified window.")
        if plan:
            print("\n".join(["", "=== Flash summary ==="] +
                             batch_summary_lines(plan, completed, "Interrupted.")))
        return 130
    except (FatalError, TransferError, OSError, serial.SerialException) as exc:
        if prog.active:
            prog.end()
        print("\nError: %s" % exc, file=sys.stderr)
        if plan:
            print("\n".join(["", "=== Flash summary ==="] +
                             batch_summary_lines(plan, completed, "Stopped after an error.")))
        return 1
    finally:
        if con is not None:
            con.close()


def print_help():
    print("""fenix-flash %s

Commands:
  help             Show this help
  cli              Open the interactive terminal interface

Command-only options:
  -a, --adapter    Serial adapter device, for example /dev/ttyACM0
  -b, --baud       Serial baud rate (default: %d)
  -f, --file       Image file to write; repeat for several partitions
  -p, --partition  Target partition name/number; repeat in the same order as -f
  -l, --list-partitions  List available eMMC partitions and exit
      --ram        U-Boot RAM staging address (default: 0x%X)
      --verify-ram U-Boot RAM address used to read back and verify (default: 0x%X)
      --restart    Discard the saved flash state and start from the beginning
      --allow-critical  Allow writing boot-critical partitions (%s)
  -y, --yes        Skip the confirmation prompt
      --version    Show version

Examples:
  python fenix-flash.py help
  python fenix-flash.py cli
  python fenix-flash.py -a /dev/ttyACM0 -l
  python fenix-flash.py -a /dev/ttyACM0 -p boot -f ./boot.img
  python fenix-flash.py -a /dev/ttyACM0 -p boot -f boot.img -p dtbo -f dtbo.img
  python fenix-flash.py -a /dev/ttyACM0 -p uboot -f uboot.img --allow-critical

With no arguments, this help is shown. Use 'cli' to open the interactive interface.
Running as root (su or sudo) is recommended.""" % (APP_VERSION, BAUD, RAM_ADDR, VERIFY_RAM_ADDR,
                                                    ", ".join(CRITICAL_PARTITIONS)))


def parse_args():
    argv = sys.argv[1:]

    if not argv or argv in (["-h"], ["--help"]):
        print_help()
        raise SystemExit(0)

    if argv[0].lower() == "help":
        if len(argv) > 1:
            print("Error: invalid command: %s" % " ".join(argv[1:]), file=sys.stderr)
            print_help()
            raise SystemExit(2)
        print_help()
        raise SystemExit(0)

    force_cli = argv[0].lower() == "cli"

    if force_cli:
        argv = argv[1:]
    elif not argv[0].startswith("-"):
        print("Error: invalid command: %s" % argv[0], file=sys.stderr)
        print_help()
        raise SystemExit(2)

    class HelpParser(argparse.ArgumentParser):
        def error(self, message):
            print("Error: invalid command or arguments: %s" % message, file=sys.stderr)
            print_help()
            raise SystemExit(2)

    parser = HelpParser(
        prog=APP_NAME,
        description="eMMC partition flasher for the %s." % TARGET,
        epilog="Use 'python fenix-flash.py help' for commands and examples."
    )
    parser.add_argument("-a", "--adapter", "--port", dest="adapter",
                        help="serial adapter device")
    parser.add_argument("-b", "--baud", type=int, default=BAUD,
                        help="serial baud rate (default: %(default)s)")
    parser.add_argument("-f", "--file", dest="files", action="append",
                        help="image file to write; repeat for several partitions")
    parser.add_argument("-p", "--partition", dest="partitions", action="append",
                        help="target partition name/number; repeat in the same order as -f")
    parser.add_argument("-l", "--list-partitions", action="store_true",
                        help="list available eMMC partitions and exit")
    parser.add_argument("--ram", type=lambda x: int(x, 0), default=RAM_ADDR,
                        help="RAM address used as staging area")
    parser.add_argument("--verify-ram", type=lambda x: int(x, 0), default=VERIFY_RAM_ADDR,
                        help="RAM address used to read back and verify")
    parser.add_argument("--restart", action="store_true",
                        help="discard the saved flash state and start from the beginning")
    parser.add_argument("--allow-critical", action="store_true",
                        help="allow writing boot-critical partitions")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="skip the confirmation prompt")
    parser.add_argument("--version", action="version",
                        version="%s %s" % (APP_NAME, APP_VERSION))

    args = parser.parse_args(argv)

    if force_cli and (args.adapter is not None or args.files or args.partitions or args.list_partitions):
        parser.error("the 'cli' command cannot be combined with command-only selection options")

    if abs(args.verify_ram - args.ram) < WINDOW_BYTES:
        parser.error("--ram and --verify-ram must be at least %d MiB apart" % (WINDOW_BYTES // MIB))

    args.interactive = force_cli

    if not args.interactive:
        if args.adapter is None:
            parser.error("-a/--adapter is required in command-only mode")
        if not args.list_partitions:
            if not args.partitions or not args.files:
                parser.error("specify -f/--file and -p/--partition, or -l/--list-partitions")
            if len(args.files) != len(args.partitions):
                parser.error("each -p/--partition needs a matching -f/--file, in the same order")

    return args


def main():
    args = parse_args()
    ui = UI()
    try:
        if args.interactive:
            return app(ui, args)
        return command_app(args, ui)
    except KeyboardInterrupt:
        ui.show_cursor()
        print("\n  Interrupted. Progress was saved up to the last verified window; run %s again to resume." % APP_NAME)
        return 130
    finally:
        ui.show_cursor()


if __name__ == "__main__":
    sys.exit(main())
