"""B6: .env.example lists exactly the variables the code reads, each with a one-line comment."""
import re

from pipeline.config import ROOT, SECRET_NAMES

CODE_DIRS = ["pipeline", "dashboard"]


def _env_example():
    lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    keys = {}
    for i, ln in enumerate(lines):
        m = re.match(r"^([A-Z][A-Z0-9_]*)=", ln)
        if m:
            keys[m.group(1)] = lines[i - 1] if i else ""
    return keys


def _code_reads():
    names, raw_env = set(), []
    files = [ROOT / "run.py"] + [p for d in CODE_DIRS for p in (ROOT / d).rglob("*.py")]
    for p in files:
        src = p.read_text(encoding="utf-8")
        names |= set(re.findall(r'secret\(\s*"([A-Z0-9_]+)"\s*\)', src))
        for m in re.finditer(r"os\.environ|getenv\(", src):
            raw_env.append(f"{p.name}:{src[:m.start()].count(chr(10)) + 1}")
    return names, raw_env


def test_env_example_matches_code_exactly():
    keys = _env_example()
    names, raw_env = _code_reads()
    assert set(keys) == names == set(SECRET_NAMES), (set(keys), names, set(SECRET_NAMES))
    # the only direct environment access is inside config.secret()
    assert len(raw_env) == 1 and raw_env[0].startswith("config.py"), raw_env


def test_each_variable_has_a_comment_saying_what_and_where():
    for k, comment in _env_example().items():
        assert comment.startswith("# ") and len(comment) > 30, k
        assert ";" in comment, f"{k}: comment should say what it is; where to get it"


def test_readme_uses_the_same_names():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for k in SECRET_NAMES:
        assert k in readme, k
