import subprocess
import sys
import threading
import itertools
import time
import re

try:
    import termios
    import tty
except ImportError:
    termios = None
    tty = None

# Colors & Typography
GREEN = "\033[0;32m"
YELLOW = "\033[1;33m"
BLUE = "\033[0;34m"
RED = "\033[0;31m"
CYAN = "\033[0;36m"
MAGENTA = "\033[0;35m"
BOLD = "\033[1m"
DIM = "\033[2m"
NC = "\033[0m"

# Semantic Icons
ICON_SUCCESS = f"{GREEN}✓{NC}"
ICON_ERROR = f"{RED}✗{NC}"
ICON_WARN = f"{YELLOW}!{NC}"
ICON_INFO = f"{CYAN}i{NC}"
ICON_ACTION = f"{MAGENTA}→{NC}"


def log(msg: str):
    print(f"[{ICON_INFO}] {msg}")


def log_change(msg: str):
    print(f"[{ICON_ACTION}] {msg}")


def log_warn(msg: str):
    print(f"[{ICON_WARN}] {YELLOW}Warning:{NC} {msg}", file=sys.stderr)


def log_error(msg: str):
    print(f"[{ICON_ERROR}] {RED}Error:{NC} {msg}", file=sys.stderr)


def log_success(msg: str):
    print(f"[{ICON_SUCCESS}] {GREEN}{msg}{NC}")


def log_git(msg: str):
    print(f"[{ICON_ACTION}] {DIM}GIT:{NC} {msg}", file=sys.stderr)


class Spinner:
    def __init__(self, message="Processing..."):
        self.spinner = itertools.cycle(
            ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        )
        self.message = message
        self.running = False
        self.thread = None

    def _spin(self):
        while self.running:
            sys.stdout.write(f"\r{CYAN}{next(self.spinner)}{NC} {self.message}")
            sys.stdout.flush()
            time.sleep(0.1)

    def __enter__(self):
        if not sys.stdout.isatty():
            print(f"[{ICON_INFO}] {self.message}...")
            return self
        self.running = True
        self.thread = threading.Thread(target=self._spin)
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if not sys.stdout.isatty():
            return
        self.running = False
        if self.thread:
            self.thread.join()
        sys.stdout.write("\r\033[K")  # Clear the line
        sys.stdout.flush()


def run_cmd(args, cwd=None, capture=True, check=False):
    res = subprocess.run(args, cwd=cwd, text=True, capture_output=capture, check=False)
    if check and res.returncode != 0:
        raise subprocess.CalledProcessError(
            res.returncode, args, output=res.stdout, stderr=res.stderr
        )
    return res


def read_single_key(prompt: str, valid_choices: str) -> str:
    valid = {choice.lower() for choice in valid_choices}

    # Highlight choices in prompt
    formatted_prompt = prompt
    for char in valid_choices:
        formatted_prompt = formatted_prompt.replace(
            f"[{char}/", f"[{BOLD}{CYAN}{char}{NC}/"
        )
        formatted_prompt = formatted_prompt.replace(
            f"/{char}/", f"/{BOLD}{CYAN}{char}{NC}/"
        )
        formatted_prompt = formatted_prompt.replace(
            f"/{char}]", f"/{BOLD}{CYAN}{char}{NC}]"
        )
        formatted_prompt = formatted_prompt.replace(
            f"[{char}]", f"[{BOLD}{CYAN}{char}{NC}]"
        )
        formatted_prompt = formatted_prompt.replace(
            f"[{char.upper()}/", f"[{BOLD}{CYAN}{char.upper()}{NC}/"
        )
        formatted_prompt = formatted_prompt.replace(
            f"/{char.upper()}/", f"/{BOLD}{CYAN}{char.upper()}{NC}/"
        )
        formatted_prompt = formatted_prompt.replace(
            f"/{char.upper()}]", f"/{BOLD}{CYAN}{char.upper()}{NC}]"
        )
        formatted_prompt = formatted_prompt.replace(
            f"[{char.upper()}]", f"[{BOLD}{CYAN}{char.upper()}{NC}]"
        )

    while True:
        if termios is not None and tty is not None and sys.stdin.isatty():
            print(formatted_prompt, end=" ", flush=True)
            fd = sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)
            try:
                tty.setraw(fd)
                choice = sys.stdin.read(1).lower()
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
            print(f"{CYAN}{choice}{NC}")
        else:
            choice = input(f"{formatted_prompt} ").strip().lower()[:1]

        if choice in valid:
            return choice

        print(f"[{ICON_ERROR}] Invalid choice: {choice}")


def parse_bool_env(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def print_table(headers, rows):
    """Draws a simple ASCII table."""
    if not rows and not headers:
        return

    ansi_escape = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

    # Calculate column widths
    col_widths = [len(ansi_escape.sub("", str(h))) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            clean_cell = ansi_escape.sub("", str(cell))
            col_widths[i] = max(col_widths[i], len(clean_cell))

    # Build the separator line
    separator = "+" + "+".join("-" * (w + 2) for w in col_widths) + "+"

    print(separator)
    # Print headers
    header_row = (
        "|"
        + "|".join(
            f" {str(h)}{' ' * (w - len(ansi_escape.sub('', str(h))))} "
            for h, w in zip(headers, col_widths)
        )
        + "|"
    )
    print(header_row)
    print(separator)

    # Print rows
    for row in rows:
        formatted_cells = []
        for i, cell in enumerate(row):
            clean_cell = ansi_escape.sub("", str(cell))
            padding = col_widths[i] - len(clean_cell)
            formatted_cells.append(f" {str(cell)}{' ' * padding} ")
        print("|" + "|".join(formatted_cells) + "|")

    print(separator)
