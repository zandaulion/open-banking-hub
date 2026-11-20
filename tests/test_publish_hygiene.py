"""Nothing personal may be committed: this fails if tracked files look like they hold real data."""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWED_IBANS = {"RO49AAAA1B31007593840000"}              # the textbook example used in tests and docs
# Check digits "00" never occur in a real IBAN (valid ones are 02-98), so XX00... marks a placeholder.
PLACEHOLDER_IBAN = re.compile(r"^[A-Z]{2}00")
PATTERNS = {
    "IBAN": re.compile(r"\b[A-Z]{2}\d{2}[A-Z]{4}[A-Z0-9]{10,24}\b"),
    "private hostname": re.compile(r"\btail[0-9a-f]{4,}\.ts\.net\b"),       # real tailnet names: tail + hex
    "email address": re.compile(r"[\w.+-]+@(?!users\.noreply\.github\.com|example\.(?:com|org))[\w-]+\.[\w.]+"),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "home path": re.compile(r"/home/[a-z][a-z0-9_-]*/"),
}
SKIP = {"uv.lock", "tests/test_publish_hygiene.py"}


def tracked_files():
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    for name in out.splitlines():
        p = ROOT / name
        if name in SKIP or not p.is_file() or p.suffix in {".png"}:
            continue
        yield name, p.read_text(errors="ignore")


def test_no_personal_data_in_tracked_files():
    problems = []
    for name, text in tracked_files():
        for kind, pat in PATTERNS.items():
            for m in pat.finditer(text):
                if kind == "IBAN" and (m.group(0) in ALLOWED_IBANS or PLACEHOLDER_IBAN.match(m.group(0))):
                    continue
                problems.append(f"{name}: {kind}")
    assert not problems, "\n".join(sorted(set(problems)))


def test_every_source_file_is_tracked():
    """A file that exists only on the developer's disk works locally and breaks every fresh clone."""
    tracked = set(subprocess.run(["git", "ls-files", "src"], cwd=ROOT, capture_output=True, text=True,
                                 check=True).stdout.splitlines())
    on_disk = {str(p.relative_to(ROOT)) for p in (ROOT / "src").rglob("*")
               if p.is_file() and "__pycache__" not in p.parts and not p.name.endswith(".egg-info")
               and ".egg-info" not in str(p)}
    missing = sorted(on_disk - tracked)
    assert not missing, "not in git: " + ", ".join(missing)
