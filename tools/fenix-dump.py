#!/usr/bin/env python3
"""fenix-dump: interactive eMMC partition dumper for the HY300 Lite over the U-Boot serial console."""

import argparse
import atexit
import glob
import hashlib
import json
import os
import random
import re
import select
import shutil
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
APP_NAME = "fenix-dump"
APP_VERSION = "1.2.0"
TARGET = "HY300 Lite (fenix)"
LOG_FILE = "fenix-dump.log"

BAUD = 1_500_000
PROMPT = "=>"
DEVICE_GLOBS = ("/dev/ttyACM*", "/dev/ttyUSB*")
MMC_DEV = 0
RAM_ADDR = 0x10000000

MIB = 1024 * 1024
SECTOR = 512
ROW = 16
WINDOW_BYTES = 32 * MIB
OVERLAP_BYTES = 1 * MIB
CHECKPOINT_BYTES = 1 * MIB
BACKTRACK_LIMIT = 16 * MIB

BLOCK_LEVELS = (512, 1024, 2048, 4096, 8192, 16384, 32768, 49152)
TARGET_LOSS = 0.004
SWITCH_MARGIN = 0.05
EVAL_BYTES = 256 * 1024
COOLDOWN_BYTES = 4 * MIB
EWMA_ALPHA = 0.3

FAIL_RETRIES = 3
REPAIR_ROUNDS = 6
REPAIR_RUN_ROWS = 32
REPAIR_BATCH = 6
PART_READ_TRIES = 6
TX_CHUNK = 8
TX_DELAY = 0.0003

EST_RATE = 26 * 1024
LONG_DUMP_SECONDS = 2 * 3600
SPEED_WINDOW_S = 15
UI_REFRESH = 0.25
POPUP_SECONDS = 5

ROOT_NOTICE = ("fenix-dump is recommended to run as root (su or sudo).",
               "Root always has access to the USB-TTL adapter and can write to any output folder.",
               "Without it, adapters and folders you cannot access will be refused.")

MD_LINE = re.compile(r"^([0-9a-fA-F]{8,16}): ((?:[0-9a-fA-F]{8} ){3}[0-9a-fA-F]{8})( +.*)$")
CRC_LINE = re.compile(r"==>\s*([0-9a-fA-F]{8})[ \t]*$", re.M)
PART_LINE = re.compile(r'^\s*(\d+)\s+0x([0-9a-fA-F]+)\s+0x([0-9a-fA-F]+)\s+"([^"]*)"\s*$')


class TransferError(Exception):
    pass


class CRCMismatch(TransferError):
    pass


class ConsoleTimeout(TransferError):
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


class Partition:
    def __init__(self, number, start, end, name):
        self.number, self.start, self.end, self.name = number, start, end, name

    @property
    def sectors(self):
        return self.end - self.start + 1

    @property
    def size(self):
        return self.sectors * SECTOR


def output_paths(final):
    return (final.with_name(final.name + ".part"), final.with_name(final.name + ".part.state"))


def load_state(path, part):
    try:
        meta = json.loads(path.read_text())
        offset = int(meta["offset"])
        same = (meta["version"] == 1 and meta["partition"] == part.name
                and meta["start"] == part.start and meta["sectors"] == part.sectors)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not same or offset < 0 or offset > part.size or offset % SECTOR:
        return None
    return offset


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

    def _send(self, data):
        for i in range(0, len(data), TX_CHUNK):
            self.ser.write(data[i:i + TX_CHUNK])
            self.ser.flush()
            time.sleep(TX_DELAY)

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

    def run(self, cmd, timeout=30.0, check_echo=False, recover=True):
        self.seq += 1
        tag = "SYNC%s_%d" % (self.session, self.seq)
        line = "%s; echo %s" % (cmd, tag)
        marker = ("\n" + tag).encode()
        self.ser.reset_input_buffer()
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
        if check_echo and line not in text:
            raise TransferError("command echo mismatch")
        return text


# transfer engine
class Engine:
    def __init__(self, con, prog, ram):
        self.con, self.prog, self.ram = con, prog, ram
        self.block = AdaptiveBlock()
        self.lost = 0
        self.retries = 0
        self.recovery = None
        self.part = None
        self.partf = None
        self.statef = None
        self.offset = 0
        self.last_ckpt = 0
        self.resumed = False

    def md_timeout(self, length):
        return 10.0 + (length / ROW) * 70 * 10 / self.con.baud * 6

    def run_checked(self, cmd, timeout, ok):
        last = ""
        for attempt in range(1, 4):
            try:
                text = self.con.run(cmd, timeout, check_echo=True)
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

    def read_chunk(self, addr, length):
        cmd = "md.l 0x%x 0x%x; crc32 0x%x 0x%x" % (addr, length // 4, addr, length)
        text = self.con.run(cmd, self.md_timeout(length))
        rows = parse_rows(text, addr, length)
        match = CRC_LINE.search(text)
        expected = int(match.group(1), 16) if match else None

        wanted = range(addr, addr + length, ROW)
        missing = [a for a in wanted if a not in rows]
        lost = len(missing)

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
        return data, lost

    def fetch(self, addr, length):
        failed, last = False, None
        for attempt in range(1, FAIL_RETRIES + 1):
            try:
                data, lost = self.read_chunk(addr, length)
                return data, lost, failed
            except TransferError as exc:
                failed, last = True, exc
                self.retries += 1
                self.prog.event("retry %d/%d at 0x%x: %s" % (attempt, FAIL_RETRIES, addr, exc))
                time.sleep(0.05 * attempt)
        half = (length // 2 // ROW) * ROW
        if half < ROW or half >= length:
            if isinstance(last, CRCMismatch):
                raise CRCMismatch("Block at 0x%x could not be validated: %s" % (addr, last))
            raise FatalError("Block at 0x%x could not be validated: %s" % (addr, last))
        left = self.fetch(addr, half)
        right = self.fetch(addr + half, length - half)
        return left[0] + right[0], left[1] + right[1], True

    def validate_partition_block(self, offset, length):
        if (offset < 0 or length <= 0 or offset % SECTOR or length % SECTOR
                or offset + length > self.part.size):
            raise FatalError("Invalid CRC recovery range.")
        sectors = length // SECTOR
        lba = self.part.start + offset // SECTOR
        pattern = re.compile(r"\b%d\s+blocks read:\s*OK" % sectors)
        self.run_checked("mmc read 0x%x 0x%x 0x%x" % (self.ram, lba, sectors), 300,
                         lambda t: bool(pattern.search(t)))
        self.fetch(self.ram, length)

    def recover_crc_mismatch(self, out, start_offset, pos, size, error):
        failure = start_offset + pos
        step = max(SECTOR, (size // SECTOR) * SECTOR)
        recovery = self.recovery

        if recovery and abs(failure - recovery["failure"]) <= max(step, recovery["step"]):
            failure = recovery["failure"]
            step = recovery["step"]
            candidate = recovery["anchor"]
        else:
            self.recovery = {"failure": failure, "step": step, "anchor": failure}
            candidate = failure - step

        self.prog.event("CRC mismatch at 0x%x: %s" % (failure, error))
        self.prog.status("CRC mismatch: searching backwards (limit 16 MiB)")
        checked = {}

        def valid(offset):
            if offset in checked:
                return checked[offset]
            try:
                self.validate_partition_block(offset, step)
            except TransferError as exc:
                checked[offset] = False
                self.prog.event("CRC recovery: block 0x%x rejected: %s" % (offset, exc))
            except FatalError as exc:
                if "could not be validated" not in str(exc):
                    raise
                checked[offset] = False
                self.prog.event("CRC recovery: block 0x%x rejected: %s" % (offset, exc))
            else:
                checked[offset] = True
                self.prog.event("CRC recovery: block 0x%x validated" % offset)
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
                "CRC mismatch recovery could not find two consecutive valid blocks "
                "within 16 MiB before partition offset 0x%x. The partial dump is not trustworthy."
                % failure
            )

        self.recovery.update({"failure": failure, "step": step, "anchor": resume})
        self.prog.event("CRC recovery: rewinding from 0x%x to 0x%x" % (self.offset, resume))
        out.flush()
        out.truncate(resume)
        out.seek(resume)
        self.offset = resume
        self.checkpoint(out)
        self.resumed = True
        self.prog.hist = deque([(time.monotonic(), self.offset)])
        self.prog.update(self.offset, self.block.size, self.lost, self.retries, self.con.resyncs)

    def save_state(self):
        tmp = Path(str(self.statef) + ".tmp")
        with tmp.open("w") as handle:
            json.dump({"version": 1, "partition": self.part.name, "start": self.part.start,
                       "sectors": self.part.sectors, "offset": self.offset}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.statef)

    def checkpoint(self, out):
        out.flush()
        os.fsync(out.fileno())
        self.save_state()
        self.last_ckpt = self.offset

    def dump_window(self, out):
        part = self.part
        start_offset = self.offset
        length = min(WINDOW_BYTES, part.size - start_offset)
        overlap = min(start_offset, OVERLAP_BYTES) if self.resumed else 0
        self.resumed = False
        windows = -(-part.size // WINDOW_BYTES)
        label = "Window %d/%d" % (start_offset // WINDOW_BYTES + 1, windows)

        # load the window into RAM
        self.prog.status("%s: loading into RAM" % label)
        lba = part.start + (start_offset - overlap) // SECTOR
        sectors = (overlap + length) // SECTOR
        pattern = re.compile(r"\b%d\s+blocks read:\s*OK" % sectors)
        self.run_checked("mmc read 0x%x 0x%x 0x%x" % (self.ram, lba, sectors), 300,
                         lambda t: bool(pattern.search(t)))

        # verify the resume point against the device
        if overlap:
            self.prog.status("%s: verifying resume point" % label)
            out.seek(start_offset - overlap)
            local = out.read(overlap)
            out.seek(start_offset)
            if zlib.crc32(local) & 0xFFFFFFFF != self.stable_crc(self.ram, overlap):
                raise FatalError("The partial file does not match the device.\nStart over to dump this partition again.")

        # reference checksum for the whole window
        base = self.ram + overlap
        self.prog.status("%s: computing reference checksum" % label)
        reference = self.stable_crc(base, length)

        # pull the window in adaptive blocks
        self.prog.status("%s: reading" % label)
        crc, pos = 0, 0
        while pos < length:
            size = min(self.block.size, length - pos)
            began = time.monotonic()
            try:
                data, lost, failed = self.fetch(base + pos, size)
            except CRCMismatch as exc:
                self.recover_crc_mismatch(out, start_offset, pos, size, exc)
                return
            # Clear stale retry/error text after a block is fetched successfully.
            if self.prog.event_text != "-":
                self.prog.event_text = "-"
            absolute_pos = start_offset + pos
            if (self.recovery
                    and absolute_pos < self.recovery['failure'] + self.recovery['step']
                    and absolute_pos + size > self.recovery['failure']):
                self.recovery = None
            elapsed = time.monotonic() - began
            out.write(data)
            crc = zlib.crc32(data, crc)
            pos += size
            self.offset += size
            self.lost += lost
            self.block.update(size, size // ROW, lost, elapsed, failed)
            if self.offset - self.last_ckpt >= CHECKPOINT_BYTES:
                self.checkpoint(out)
            self.prog.update(self.offset, self.block.size, self.lost, self.retries, self.con.resyncs)

        if crc & 0xFFFFFFFF != reference:
            self.offset = start_offset
            raise FatalError("Window checksum mismatch; RAM may have changed during the dump.")

    def dump(self, part, final, offset):
        self.part = part
        self.partf, self.statef = output_paths(final)
        self.offset = self.last_ckpt = offset
        self.resumed = offset > 0
        self.con.idle = self.prog.tick

        mode = "r+b" if self.partf.exists() else "w+b"
        with self.partf.open(mode) as out:
            out.truncate(offset)
            out.seek(offset)
            self.checkpoint(out)
            try:
                while self.offset < part.size:
                    self.dump_window(out)
            finally:
                try:
                    self.checkpoint(out)
                except (OSError, ValueError):
                    pass

        # final verification
        self.prog.status("Verifying the output file")
        if self.partf.stat().st_size != part.size:
            raise FatalError("Output file has an unexpected size.")
        sha = hashlib.sha256()
        with self.partf.open("rb") as handle:
            while True:
                block = handle.read(MIB)
                if not block:
                    break
                sha.update(block)
        os.replace(self.partf, final)
        self.statef.unlink(missing_ok=True)
        digest = sha.hexdigest()
        final.with_name(final.name + ".sha256").write_text("%s  %s\n" % (digest, final.name))
        self.con.idle = None
        return digest


# terminal ui
def read_key(timeout=None):
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        if timeout is not None and not select.select([fd], [], [], timeout)[0]:
            return None
        key = os.read(fd, 1)
        if key == b"\x1b" and select.select([fd], [], [], 0.05)[0]:
            key += os.read(fd, 2)
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
        print(self.paint("  eMMC partition dumper for the %s over U-Boot serial" % TARGET, "2"))
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
        self.final = None
        self.t0 = time.monotonic()
        self.hist = deque()

    def begin(self, part, final, offset):
        self.part, self.final = part, final
        self.total = part.size
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
            row("Output", str(self.final)),
            "",
            bar,
            "",
            row("Size", "%s / %s" % (fmt_size(self.done), fmt_size(self.total))),
            row("Speed", "%s/s" % fmt_size(speed)),
            row("Remaining", fmt_time(remaining) if remaining is not None else "--:--:--"),
            row("Elapsed", fmt_time(now - self.t0)),
            row("Block size", fmt_size(self.block) if self.block else "-"),
            row("Repairs", "%d rows   (retries %d, resyncs %d)" % (self.lost, self.retries, self.resyncs)),
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


def prepare_output(ui, part, final):
    partf, statef = output_paths(final)
    if final.exists() and not ui.confirm("%s already exists. Overwrite it?" % final.name):
        return None
    if not partf.exists() and not statef.exists():
        return 0

    offset = load_state(statef, part) if partf.exists() else None
    if offset and partf.stat().st_size >= offset:
        choice = ui.menu("A partial dump of '%s' was found" % part.name,
                         ["Resume from %s (%.1f%%)" % (fmt_size(offset), 100.0 * offset / part.size),
                          "Start over", "Cancel"])
        if choice is None or choice == 2:
            return None
        if choice == 0:
            return offset
    elif not ui.confirm("Leftover partial files were found. Delete them and start over?"):
        return None
    partf.unlink(missing_ok=True)
    statef.unlink(missing_ok=True)
    return 0


def print_partition_table(parts):
    """Print partition information obtained from the connected device."""
    print("Available eMMC partitions (%d):" % len(parts))
    print("%-4s %-20s %12s %12s %12s %11s" %
          ("NUM", "NAME", "SIZE", "START LBA", "END LBA", "EST. TIME"))
    print("-" * 78)
    for part in parts:
        print("%-4d %-20s %12s 0x%08X 0x%08X %11s" %
              (part.number, part.name, fmt_size(part.size), part.start,
               part.end, fmt_time(part.size / EST_RATE)))
    print("\nEstimated times use %s/s and are only rough estimates." % fmt_size(EST_RATE))


def resolve_partition_selector(parts, selector):
    """Resolve a comma-separated list of partition names and/or numbers."""
    tokens = [token.strip() for token in selector.split(",")]
    if not tokens or any(not token for token in tokens):
        raise FatalError("Invalid -p selector: use comma-separated names/numbers, e.g. boot,uboot,4.")

    selected = []
    seen = set()
    for token in tokens:
        if token.isdigit():
            part = next((p for p in parts if p.number == int(token)), None)
        else:
            part = next((p for p in parts if p.name.casefold() == token.casefold()), None)
        if part is None:
            available = ["%d: %s (%s)" % (p.number, p.name, fmt_size(p.size)) for p in parts]
            raise FatalError("Partition not found: %s\nAvailable partitions:\n%s" %
                             (token, "\n".join(available)))
        if part.number in seen:
            raise FatalError("Partition selected more than once: %s" % token)
        seen.add(part.number)
        selected.append(part)
    return selected


def verify_existing_dump(final, part):
    """Return True only if an existing image has the expected size and valid SHA-256 sidecar."""
    if not final.is_file():
        return False
    try:
        if final.stat().st_size != part.size:
            return False
        fields = final.with_name(final.name + ".sha256").read_text().split()
        if len(fields) < 2 or fields[-1] != final.name or not re.fullmatch(r"[0-9a-fA-F]{64}", fields[0]):
            return False
        expected = fields[0].lower()
    except OSError:
        return False

    digest = hashlib.sha256()
    try:
        with final.open("rb") as handle:
            while True:
                block = handle.read(MIB)
                if not block:
                    break
                digest.update(block)
    except OSError:
        return False
    return digest.hexdigest() == expected


def batch_summary_lines(parts, completed, reason=None, skipped=None):
    """Build a stable summary of completed, verified-existing, and unfinished items."""
    skipped = skipped or []
    accounted = {part.number for part in completed} | {part.number for part in skipped}
    missing = [part for part in parts if part.number not in accounted]
    lines = []
    if reason:
        lines.append("Reason: %s" % reason)
        lines.append("")
    lines.append("Dumped successfully in this run (%d):" % len(completed))
    if completed:
        lines.extend("  %d: %s (%s)" % (p.number, p.name, fmt_size(p.size)) for p in completed)
    else:
        lines.append("  (none)")
    lines.append("")
    lines.append("Already complete, verified, and skipped (%d):" % len(skipped))
    if skipped:
        lines.extend("  %d: %s (%s)" % (p.number, p.name, fmt_size(p.size)) for p in skipped)
    else:
        lines.append("  (none)")
    lines.append("")
    lines.append("Not completed (%d):" % len(missing))
    if missing:
        lines.extend("  %d: %s (%s)" % (p.number, p.name, fmt_size(p.size)) for p in missing)
        lines.append("Partial files, if present, can be resumed by running the same command again.")
    else:
        lines.append("  (none)")
    return lines


def print_batch_summary(parts, completed, reason=None):
    print("\n=== Partition dump summary ===")
    for line in batch_summary_lines(parts, completed, reason):
        print(line)


def dump_cli_sequence(ui, engine, prog, out_dir, parts):
    """Dump multiple partitions from the interactive UI, stopping with a summary."""
    completed = []
    skipped = []
    current = None
    try:
        for part in parts:
            final = out_dir / (part.name + ".img")
            if final.exists() and verify_existing_dump(final, part):
                skipped.append(part)

        # Estimate required *additional* space before starting a multi-day run.
        required_total = 64 * MIB
        for part in parts:
            if part in skipped:
                continue
            final = out_dir / (part.name + ".img")
            if final.exists():
                required_total += part.size
                continue
            partf, statef = output_paths(final)
            offset = load_state(statef, part) if partf.exists() and statef.exists() else None
            if offset is None or not partf.exists() or partf.stat().st_size < offset:
                offset = 0
            required_total += part.size - offset
        free = shutil.disk_usage(out_dir).free
        if free < required_total:
            raise FatalError("Not enough free disk space for the batch: %s required, %s available." %
                             (fmt_size(required_total), fmt_size(free)))

        for current in parts:
            final = out_dir / (current.name + ".img")
            if current in skipped:
                print("Already complete and SHA-256 verified; skipping %s." % current.name)
                continue
            offset = prepare_output(ui, current, final)
            if offset is None:
                ui.message("Batch dump cancelled",
                           batch_summary_lines(parts, completed,
                                               "Cancelled before completing partition '%s'." % current.name,
                                               skipped=skipped))
                return 0

            required = current.size - offset + 64 * MIB
            free = shutil.disk_usage(out_dir).free
            if free < required:
                raise FatalError("Not enough free disk space for %s: %s required, %s available." %
                                 (current.name, fmt_size(required), fmt_size(free)))

            prog.begin(current, final, offset)
            digest = engine.dump(current, final, offset)
            prog.end()
            elapsed = time.monotonic() - prog.t0
            completed.append(current)
            log("Batch dump complete: %s sha256=%s" % (final, digest))
            log("Batch dump time: %s average=%s/s" %
                (fmt_time(elapsed), fmt_size((current.size - offset) / max(elapsed, 1e-3))))

        ui.message("Partition batch finished", batch_summary_lines(parts, completed, skipped=skipped))
        return 0
    except KeyboardInterrupt:
        if prog.active:
            prog.end()
        ui.message("Batch dump interrupted",
                   batch_summary_lines(parts, completed,
                                       "Interrupted while dumping '%s'." % current.name if current else "Interrupted.",
                                       skipped=skipped),
                   error=True)
        return 130
    except (FatalError, TransferError, OSError) as exc:
        if prog.active:
            prog.end()
        ui.message("Batch dump stopped",
                   [str(exc), ""] + batch_summary_lines(
                       parts, completed,
                       "Stopped while processing '%s'." % current.name if current else "Stopped.",
                       skipped=skipped),
                   error=True)
        return 1


def session(ui, args, engine, prog, out_dir):
    con = engine.con
    try:
        ui.task("Syncing with the U-Boot console", lambda: con.resync(initial=True))
        ui.task("Selecting eMMC device %d" % MMC_DEV, engine.select_mmc)
        parts = ui.task("Reading the partition table", engine.read_partitions)
    except (FatalError, TransferError) as exc:
        ui.message("Setup failed", str(exc).split("\n"), error=True)
        return 1

    time.sleep(0.4)
    last = 0
    all_index = len(parts)
    quit_index = len(parts) + 1
    while True:
        options = ["%2d  %-14s %10s   LBA 0x%08X-0x%08X   ~%s" %
                   (p.number, p.name, fmt_size(p.size), p.start, p.end, fmt_time(p.size / EST_RATE))
                   for p in parts]
        options += ["Dump all partitions (smallest to largest)", "Quit"]
        choice = ui.menu("Select the partition to dump", options, default=last,
                         note="Estimated times assume about %s/s." % fmt_size(EST_RATE))
        if choice is None or choice == quit_index:
            return 0

        if choice == all_index:
            ordered = sorted(parts, key=lambda part: (part.size, part.number))
            total_size = sum(part.size for part in ordered)
            total_seconds = total_size / EST_RATE
            warning = [
                "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
                "WARNING: FULL PARTITION DUMP",
                "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
                "",
                "This will read every listed eMMC partition, from smallest to largest.",
                "It may run for many hours or several days over the serial connection.",
                "The UART/U-Boot session can stall or disconnect, and a dump can fail.",
                "Keep the projector powered and prevent the laptop from suspending.",
                "Make sure the output filesystem has enough free space.",
                "Pressing Ctrl+C stops the sequence; the summary will show what completed",
                "and what remains. A partial dump may be resumable.",
                "",
                "Partitions: %d" % len(ordered),
                "Total data: %s" % fmt_size(total_size),
                "Rough transfer estimate: %s (at %s/s, excluding overhead/retries)" %
                (fmt_time(total_seconds), fmt_size(EST_RATE)),
                "Order: %s" % ", ".join(p.name for p in ordered),
            ]
            ui.message("WARNING — Dump all partitions", warning, error=True)
            if not ui.confirm("Start dumping all partitions?", [
                    "This can take days. Continue only if power and suspend settings are handled.",
                    "No partition will be dumped unless you confirm."]):
                ui.message("Full dump cancelled",
                           batch_summary_lines(ordered, [], "Cancelled before starting; no partitions were dumped."))
                last = 0
                continue
            result = dump_cli_sequence(ui, engine, prog, out_dir, ordered)
            if result != 0:
                return result
            last = 0
            continue

        part = parts[choice]
        final = out_dir / (part.name + ".img")

        estimate = part.size / EST_RATE
        details = ["Partition : %s (#%d)" % (part.name, part.number),
                   "Size      : %s" % fmt_size(part.size),
                   "Estimated : ~%s" % fmt_time(estimate),
                   "Output    : %s" % final]
        if estimate > LONG_DUMP_SECONDS:
            details.append("Warning   : this is a very large partition and will take a long time.")
        if not ui.confirm("Dump '%s'?" % part.name, details):
            continue

        offset = prepare_output(ui, part, final)
        if offset is None:
            continue
        if shutil.disk_usage(out_dir).free < part.size - offset + 64 * MIB:
            ui.message("Not enough free disk space", ["%s needed in %s." % (fmt_size(part.size - offset), out_dir)], error=True)
            continue

        prog.begin(part, final, offset)
        try:
            digest = engine.dump(part, final, offset)
        except KeyboardInterrupt:
            prog.end()
            ui.message("Dump interrupted", ["Progress was saved.", "Run %s again to resume." % APP_NAME])
            return 130
        except (FatalError, TransferError, OSError) as exc:
            prog.end()
            ui.message("Dump failed", str(exc).split("\n") + ["", "Progress was saved up to the last checkpoint.", "Run %s again to resume." % APP_NAME], error=True)
            return 1
        prog.end()
        elapsed = time.monotonic() - prog.t0
        ui.message("Dump complete", [
            "File     : %s" % final,
            "Size     : %s" % fmt_size(part.size),
            "SHA-256  : %s" % digest,
            "Time     : %s (avg %s/s)" % (fmt_time(elapsed), fmt_size((part.size - offset) / max(elapsed, 1e-3))),
            "Repairs  : %d rows, %d retries" % (engine.lost, engine.retries),
        ])
        last = min(choice + 1, max(0, len(parts) - 1))


def app(ui, args):
    if not is_root():
        ui.notice("Running without root privileges", ROOT_NOTICE)

    out_dir = Path(args.out_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

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
        return session(ui, args, Engine(con, prog, args.ram), prog, out_dir)
    finally:
        con.close()


def command_app(args, ui):
    con = None
    prog = Progress(ui, compact=True)
    selected = []
    completed = []
    skipped = []
    batch = False
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
        engine = Engine(con, prog, args.ram)

        print("Synchronizing with U-Boot...")
        con.resync(initial=True)
        print("Selecting eMMC device %d..." % MMC_DEV)
        engine.select_mmc()
        print("Reading partition table...")
        parts = engine.read_partitions()

        if args.list_partitions:
            print_partition_table(parts)
            return 0

        if args.all_partitions:
            selected = sorted(parts, key=lambda part: (part.size, part.number))
        else:
            selected = resolve_partition_selector(parts, args.partition)
        batch = args.all_partitions or len(selected) > 1

        out_dir = Path(args.out_dir).expanduser().resolve()
        out_dir.mkdir(parents=True, exist_ok=True)

        # Resume a cancelled batch efficiently: only skip existing files when
        # their size and sidecar SHA-256 both verify against the image contents.
        work = []
        conflicts = []
        for part in selected:
            final = out_dir / (part.name + ".img")
            if final.exists() and not args.force:
                if batch and verify_existing_dump(final, part):
                    skipped.append(part)
                    print("Already complete and SHA-256 verified; skipping %s." % part.name)
                    continue
                conflicts.append(final)
            else:
                work.append(part)
        if conflicts:
            raise FatalError("Output already exists or is not verifiable: %s\nUse --force to replace it." %
                             "\n".join(str(path) for path in conflicts))

        if batch:
            required_total = 64 * MIB
            for part in work:
                final = out_dir / (part.name + ".img")
                if args.restart or final.exists():
                    remaining = part.size
                else:
                    partf, statef = output_paths(final)
                    offset = load_state(statef, part) if partf.exists() and statef.exists() else None
                    if offset is None or not partf.exists() or partf.stat().st_size < offset:
                        offset = 0
                    remaining = part.size - offset
                required_total += remaining
            free = shutil.disk_usage(out_dir).free
            if free < required_total:
                raise FatalError("Not enough free disk space for the batch: %s required, %s available." %
                                 (fmt_size(required_total), fmt_size(free)))

            print("Dump order (smallest to largest only with --all-partitions):")
            for part in selected:
                print("  %2d  %-20s %10s" % (part.number, part.name, fmt_size(part.size)))

        for part in work:
            final = out_dir / (part.name + ".img")
            partf, statef = output_paths(final)
            if args.restart:
                partf.unlink(missing_ok=True)
                statef.unlink(missing_ok=True)
                Path(str(statef) + ".tmp").unlink(missing_ok=True)
                offset = 0
            elif partf.exists() or statef.exists():
                offset = load_state(statef, part) if partf.exists() and statef.exists() else None
                if offset is None or partf.stat().st_size < offset:
                    raise FatalError("Invalid or incomplete partial dump found for '%s'. Use --restart to start over." % part.name)
                print("Resuming %s at %s (%.1f%%)..." %
                      (part.name, fmt_size(offset), 100.0 * offset / part.size))
            else:
                offset = 0

            required = part.size - offset + 64 * MIB
            free = shutil.disk_usage(out_dir).free
            if free < required:
                raise FatalError("Not enough free disk space for %s: %s required, %s available." %
                                 (part.name, fmt_size(required), fmt_size(free)))

            estimate = (part.size - offset) / EST_RATE
            print("\nPartition: %s (#%d)" % (part.name, part.number))
            print("Size: %s | Estimated: ~%s" % (fmt_size(part.size), fmt_time(estimate)))
            print("Output: %s" % final)
            print("Starting dump... Press Ctrl+C to stop; progress is checkpointed.")

            prog.begin(part, final, offset)
            digest = engine.dump(part, final, offset)
            prog.end()
            elapsed = time.monotonic() - prog.t0
            completed.append(part)
            print("Dump complete")
            print("File: %s" % final)
            print("Size: %s" % fmt_size(part.size))
            print("SHA-256: %s" % digest)
            print("Time: %s (avg %s/s)" %
                  (fmt_time(elapsed), fmt_size((part.size - offset) / max(elapsed, 1e-3))))
            print("Repairs: %d rows | Retries: %d | Resyncs: %d" %
                  (engine.lost, engine.retries, con.resyncs))

        if batch:
            print("\n".join(["", "=== Partition dump summary ==="] +
                             batch_summary_lines(selected, completed,
                                                 "Selected sequence finished.", skipped=skipped)))
        return 0
    except KeyboardInterrupt:
        if prog.active:
            prog.end()
        print("\nInterrupted. Partial progress was saved where possible.")
        if selected:
            print("\n".join(["", "=== Partition dump summary ==="] +
                             batch_summary_lines(selected, completed,
                                                 "Interrupted; unfinished partitions are listed below.",
                                                 skipped=skipped)))
        else:
            print("No partition dump was completed.")
        return 130
    except (FatalError, TransferError, OSError, serial.SerialException) as exc:
        if prog.active:
            prog.end()
        print("\nError: %s" % exc, file=sys.stderr)
        if selected and batch:
            print("\n".join(["", "=== Partition dump summary ==="] +
                             batch_summary_lines(selected, completed,
                                                 "Stopped after an error; unfinished partitions are listed below.",
                                                 skipped=skipped)))
        return 1
    finally:
        if con is not None:
            con.close()


def print_help():
    print("""fenix-dump %s

Commands:
  help             Show this help
  cli              Open the interactive terminal interface

Command-only options:
  -a, --adapter    Serial adapter device, for example /dev/ttyACM0
  -b, --baud       Serial baud rate (default: %d)
  -p, --partition  Partition name/number; multiple values separated by commas
  -l, --list-partitions  List available eMMC partitions and exit
      --all-partitions   Dump every partition, smallest to largest
  -o, --out-dir    Output directory (default: current directory)
      --ram        U-Boot RAM staging address (default: 0x%X)
      --force      Replace an existing final image
      --restart    Restart the dump from the beginning; does not reboot the device
      --version    Show version

Examples:
  python fenix-dump.py help
  python fenix-dump.py cli
  python fenix-dump.py -a /dev/ttyACM0 -b 1500000 -l
  python fenix-dump.py -a /dev/ttyACM0 -p boot,uboot,4
  python fenix-dump.py -a /dev/ttyACM0 --all-partitions -o ./dump
  python fenix-dump.py -a /dev/ttyACM0 -p uboot --restart

With no arguments, this help is shown. Use 'cli' to open the interactive interface.
Running as root (su or sudo) is recommended.""" % (APP_VERSION, BAUD, RAM_ADDR))


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
        description="eMMC partition dumper for the %s." % TARGET,
        epilog="Use 'python fenix-dump.py help' for commands and examples."
    )
    parser.add_argument("-a", "--adapter", "--port", dest="adapter",
                        help="serial adapter device")
    parser.add_argument("-b", "--baud", type=int, default=BAUD,
                        help="serial baud rate (default: %(default)s)")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("-p", "--partition",
                           help="partition name/number; comma-separated for multiple partitions")
    selection.add_argument("-l", "--list-partitions", action="store_true",
                           help="list available eMMC partitions and exit")
    selection.add_argument("--all-partitions", action="store_true",
                           help="dump every partition, smallest to largest")
    parser.add_argument("-o", "--out-dir", default=".",
                        help="directory for dumped images (default: current)")
    parser.add_argument("--ram", type=lambda x: int(x, 0), default=RAM_ADDR,
                        help="RAM address used as staging area")
    parser.add_argument("--force", action="store_true",
                        help="replace an existing final image")
    parser.add_argument(
        "--restart",
        action="store_true",
        help="discard partial files and restart the dump; does not reboot the device"
    )
    parser.add_argument("--version", action="version",
                        version="%s %s" % (APP_NAME, APP_VERSION))

    args = parser.parse_args(argv)

    if force_cli and (args.adapter is not None or args.partition is not None
                      or args.list_partitions or args.all_partitions):
        parser.error("the 'cli' command cannot be combined with command-only selection options")

    args.interactive = force_cli

    if not args.interactive:
        if args.adapter is None:
            parser.error("-a/--adapter is required in command-only mode")
        if args.partition is None and not args.list_partitions and not args.all_partitions:
            parser.error("specify one of -p/--partition, -l/--list-partitions, or --all-partitions")

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
        print("\n  Interrupted. Partial progress was saved; run %s again to resume." % APP_NAME)
        return 130
    finally:
        ui.show_cursor()


if __name__ == "__main__":
    sys.exit(main())
