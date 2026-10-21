#!/usr/bin/env python3
"""stop-uboot: interrupt U-Boot execution through a serial console."""

import argparse
import atexit
import glob
import os
import re
import select
import shutil
import sys
import termios
import time
import tty

import serial
from serial.tools import list_ports


# settings
APP_NAME = "stop-uboot"
APP_VERSION = "1.0.0"

DEFAULT_ADAPTER = "/dev/ttyACM0"
DEFAULT_BAUD = 1_500_000

DEVICE_GLOBS = ("/dev/ttyACM*", "/dev/ttyUSB*")
INTERRUPT_BYTE = b"\x03"
INTERRUPT_MARKER = "<INTERRUPT>"

TX_DELAY = 0.001
KEY_TIMEOUT = 0.05
MAX_READ_LINES = 5
READ_LIMIT = 4096
BUFFER_LIMIT = 4096
POPUP_SECONDS = 5

ROOT_NOTICE = ("stop-uboot is recommended to run as root (su or sudo).",
               "Root always has access to the USB-TTL adapter.",
               "Without it, an adapter you cannot access will be refused.")

PROMPT_LINE = re.compile(r"(?:^|\n)=>[ \t]*(?:\n)?$")


# helpers
def find_devices():
    paths = sorted({p for pattern in DEVICE_GLOBS for p in glob.glob(pattern)})
    info = {p.device: p for p in list_ports.comports()}
    return [
        (path, info[path].description if path in info else "serial device")
        for path in paths
    ]


def adapter_error(adapter):
    if not os.path.exists(adapter):
        return "Serial adapter does not exist: %s" % adapter
    if not os.access(adapter, os.R_OK | os.W_OK):
        return "Cannot access %s. Run as root or check the device permissions." % adapter
    return None


def is_root():
    return os.geteuid() == 0


# keyboard input
class Keyboard:
    def __init__(self):
        self.fd = None
        self.original = None
        self.active = sys.stdin.isatty() and sys.stdout.isatty()

    def __enter__(self):
        if self.active:
            self.fd = sys.stdin.fileno()
            self.original = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self.original is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.original)
            self.original = None

    def read(self, timeout=None):
        if not self.active:
            return None

        ready, _, _ = select.select([self.fd], [], [], timeout)
        if not ready:
            return None

        key = os.read(self.fd, 1)

        # Read arrow-key sequences without treating them as Escape.
        if key == b"\x1b":
            sequence = b""
            deadline = time.monotonic() + KEY_TIMEOUT

            while len(sequence) < 2:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break

                ready, _, _ = select.select([self.fd], [], [], remaining)
                if not ready:
                    break

                sequence += os.read(self.fd, 1)

            key += sequence

        return key


# serial console
class Console:
    def __init__(self, adapter, baud):
        self.ser = serial.Serial(
            adapter,
            baud,
            timeout=0,
            write_timeout=1,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
            exclusive=True,
        )

    def interrupt(self):
        self.ser.write(INTERRUPT_BYTE)

    def read(self):
        data = bytearray()
        lines = 0

        while lines < MAX_READ_LINES and len(data) < READ_LIMIT:
            if not self.ser.in_waiting:
                break
            byte = self.ser.read(1)
            if not byte:
                break
            data.extend(byte)
            if byte == b"\n":
                lines += 1

        return data.decode("ascii", errors="ignore")

    def close(self):
        self.ser.close()


# terminal ui
class UI:
    def __init__(self):
        self.tty = sys.stdin.isatty() and sys.stdout.isatty()
        self.color = self.tty and "NO_COLOR" not in os.environ
        self.adapter = None
        self.baud = None
        atexit.register(self.show_cursor)

    def paint(self, text, *codes):
        if not self.color:
            return text
        return "\x1b[%sm%s\x1b[0m" % (";".join(codes), text)

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
        print(self.paint("  Interrupt U-Boot execution over a serial console", "2"))
        print(self.paint("  " + "─" * max(10, min(60, self.cols() - 4)), "2"))
        print()

    def heading(self, title, note=None):
        print("  " + self.paint(title, "1"))
        for line in note.split("\n") if note else []:
            print("  " + self.paint(line, "2"))
        print()

    def message(self, title, lines, error=False):
        self.show_cursor()
        self.banner()

        print("  " + self.paint(title, "1", "31" if error else "32"))
        print()

        for line in lines:
            print("  " + line)

        print()

    def notice(self, title, lines, keyboard, seconds=POPUP_SECONDS):
        self.show_cursor()
        self.banner()

        print("  " + self.paint(title, "1", "33"))
        print()

        for line in lines:
            print("  " + line)

        print()

        if keyboard.active:
            print(self.paint("  Continuing in %d seconds (press any key to skip)" % seconds, "2"))
            keyboard.read(timeout=seconds)

    def menu(self, title, options, keyboard, default=0, note=None):
        self.banner()
        self.heading(title, note)

        count = len(options)
        if count == 0:
            return None

        default = min(max(default, 0), count - 1)

        # Numbered menu when terminal navigation is unavailable.
        if not self.tty:
            for i, option in enumerate(options, 1):
                print("  %d) %s" % (i, option))

            while True:
                try:
                    answer = input("> ").strip().lower()
                except EOFError:
                    return None

                if answer == "":
                    return default
                if answer in ("q", "quit", "exit"):
                    return None
                if answer.isdigit() and 1 <= int(answer) <= count:
                    return int(answer) - 1

        rows = shutil.get_terminal_size((80, 24)).lines
        note_lines = note.count("\n") + 1 if note else 0
        view = min(count, max(3, rows - 11 - note_lines))

        index = default
        first = True
        self.hide_cursor()

        try:
            while True:
                top = max(0, min(index - view // 2, count - view))

                if not first:
                    sys.stdout.write("\x1b[%dA" % (view + 2))
                first = False

                out = []
                for i in range(top, top + view):
                    label = ("  > " if i == index else "    ") + options[i]
                    label = label[:self.cols() - 1]
                    if i == index:
                        label = self.paint(label, "7")
                    out.append("\x1b[2K\r" + label)

                out.append("\x1b[2K\r")
                out.append(
                    "\x1b[2K\r"
                    + self.paint("  up/down: move  enter: select  q: quit", "2")
                )

                sys.stdout.write("\n".join(out) + "\n")
                sys.stdout.flush()

                key = keyboard.read()

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

    def confirm(self, question, keyboard, details=()):
        choice = self.menu(
            question,
            ["Yes", "No"],
            keyboard,
            default=1,
            note="\n".join(details) or None,
        )
        return choice == 0

    def wait_start(self, keyboard):
        self.banner()
        self.heading(
            "Ready to interrupt U-Boot",
            "Adapter : %s\nBaud    : %d" % (self.adapter, self.baud),
        )
        print("  Press Enter to start sending Ctrl+C.")
        print("  Press ESC to cancel.")
        print()

        if not keyboard.active:
            try:
                answer = input("> ").strip().lower()
            except EOFError:
                return False
            return answer not in ("q", "quit", "exit", "esc")

        while True:
            key = keyboard.read()
            if key in (b"\r", b"\n"):
                return True
            if key == b"\x1b":
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


# interrupt engine
def interrupt_loop(con, keyboard=None):
    buffer = ""

    while True:
        con.interrupt()

        response = con.read()
        if response:
            sys.stdout.write(response)
            sys.stdout.flush()

            buffer = (buffer + response.replace("\r", ""))[-BUFFER_LIMIT:]

            if INTERRUPT_MARKER in buffer or PROMPT_LINE.search(buffer):
                return True

        if keyboard is not None and keyboard.active:
            key = keyboard.read(timeout=TX_DELAY)
            if key == b"\x1b":
                return False
        else:
            time.sleep(TX_DELAY)


# application
def app(ui, args):
    if not is_root():
        with Keyboard() as keyboard:
            ui.notice("Running without root privileges", ROOT_NOTICE, keyboard)

    devices = find_devices()

    if not devices:
        ui.message(
            "No serial device found",
            [
                "Nothing matches %s." % ", ".join(DEVICE_GLOBS),
                "Check the USB-TTL bridge and try again.",
            ],
            error=True,
        )
        return 1

    preferred = next(
        (i for i, device in enumerate(devices) if device[0] == args.adapter),
        0,
    )

    with Keyboard() as keyboard:
        choice = ui.menu(
            "Select the serial device",
            ["%-16s %s" % device for device in devices],
            keyboard,
            default=preferred,
            note="Pick the USB-TTL bridge connected to the projector UART.",
        )

        if choice is None:
            return 0

        adapter = devices[choice][0]
        error = adapter_error(adapter)
        if error:
            ui.message("Serial adapter unavailable", [error], error=True)
            return 1

        ui.adapter = adapter
        ui.baud = args.baud

        if not ui.confirm(
            "Use %s at %d baud?" % (adapter, args.baud),
            keyboard,
            ["Stop the projector at the U-Boot prompt (=>) before continuing."],
        ):
            return 0

        if not ui.wait_start(keyboard):
            ui.message("Operation cancelled", ["No Ctrl+C bytes were sent."])
            return 0

        try:
            con = ui.task(
                "Opening %s" % adapter,
                lambda: Console(adapter, args.baud),
            )
        except serial.SerialException as exc:
            ui.message("Could not open the serial port", [str(exc)], error=True)
            return 1

        try:
            ui.banner()
            ui.heading(
                "Interrupting U-Boot",
                "Sending Ctrl+C (0x03) until the prompt is detected.",
            )
            print("  Press ESC to stop manually.")
            print()

            success = interrupt_loop(con, keyboard)
            print()

            if success:
                ui.message(
                    "U-Boot interrupted",
                    [
                        "Detected the U-Boot prompt or interrupt acknowledgment.",
                        "Stopped sending Ctrl+C.",
                    ],
                )
            else:
                ui.message(
                    "Interrupt loop stopped",
                    [
                        "Stopped manually by the user.",
                        "Successful interruption was not confirmed.",
                    ],
                    error=True,
                )
                return 1

            return 0
        finally:
            con.close()


def command_app(args):
    if not is_root():
        print("Warning: %s" % " ".join(ROOT_NOTICE), file=sys.stderr)

    error = adapter_error(args.adapter)
    if error:
        print("Error: %s" % error, file=sys.stderr)
        return 1

    print("%s %s - command-only mode" % (APP_NAME, APP_VERSION))
    print("Opening %s at %d baud..." % (args.adapter, args.baud))

    con = None
    try:
        con = Console(args.adapter, args.baud)

        print("Sending Ctrl+C until U-Boot responds.")
        print("Press Ctrl+C to stop the script.\n")

        success = interrupt_loop(con)
        if not success:
            return 1

        print("\n\n" + "=" * 55)
        print("U-Boot execution interrupted successfully!")
        print("=" * 55 + "\n")
        return 0
    finally:
        if con is not None:
            con.close()


# command line
def print_help():
    print(
        """%s %s

Commands:
  help             Show this help
  cli              Open the interactive U-Boot interrupt interface

Command-only options:
  -a, --adapter    Serial adapter device (default: %s)
  -b, --baud       Serial baud rate (default: %d)
      --version    Show version

Examples:
  python %s.py help
  python %s.py cli
  python %s.py -a /dev/ttyACM0
  python %s.py -a /dev/ttyACM0 -b 1500000

With no arguments, this help is shown. Use 'cli' for interactive mode.
Running as root (su or sudo) is recommended."""
        % (
            APP_NAME,
            APP_VERSION,
            DEFAULT_ADAPTER,
            DEFAULT_BAUD,
            APP_NAME,
            APP_NAME,
            APP_NAME,
            APP_NAME,
        )
    )


def parse_args():
    argv = sys.argv[1:]

    if not argv or argv in (["-h"], ["--help"]):
        print_help()
        raise SystemExit(0)

    if argv[0].lower() == "help":
        if len(argv) > 1:
            print(
                "Error: invalid command: %s" % " ".join(argv[1:]),
                file=sys.stderr,
            )
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
            print(
                "Error: invalid command or arguments: %s" % message,
                file=sys.stderr,
            )
            print_help()
            raise SystemExit(2)

    parser = HelpParser(
        prog=APP_NAME,
        description="Interrupt U-Boot execution over a serial console.",
        add_help=False,
    )

    parser.add_argument("-h", "--help", action="store_true", dest="show_help")
    parser.add_argument(
        "-a",
        "--adapter",
        "--port",
        dest="adapter",
        default=None,
        help="serial adapter device",
    )
    parser.add_argument(
        "-b",
        "--baud",
        "--baudrate",
        type=int,
        default=DEFAULT_BAUD,
        help="serial baud rate (default: %(default)s)",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="%s %s" % (APP_NAME, APP_VERSION),
    )

    args = parser.parse_args(argv)

    if args.show_help:
        print_help()
        raise SystemExit(0)

    if args.baud <= 0:
        parser.error("baud rate must be greater than zero")

    args.interactive = force_cli

    if args.adapter is None:
        args.adapter = DEFAULT_ADAPTER

    return args


def main():
    args = parse_args()
    ui = UI()

    try:
        if args.interactive:
            return app(ui, args)
        return command_app(args)
    except KeyboardInterrupt:
        if args.interactive:
            ui.message(
                "Operation interrupted",
                ["Stopped by Ctrl+C. No further interrupt bytes will be sent."],
            )
        else:
            print(
                "\nInterrupted. Stopped sending Ctrl+C.",
                file=sys.stderr,
            )
        return 130
    except (OSError, serial.SerialException) as exc:
        if args.interactive:
            ui.message("Operation failed", [str(exc)], error=True)
        else:
            print("\nError: %s" % exc, file=sys.stderr)
        return 1
    finally:
        ui.show_cursor()


if __name__ == "__main__":
    sys.exit(main())
