#!/usr/bin/env python3
"""Extract a pulled VPS full-report zip into the repo's ``vps_full_reports/latest/`` folder
so ChatGPT can inspect it directly (it reads text/JSON, not binary zips).

Run this every time a full report is pulled from the VPS:

    python scripts/save_full_report_to_repo.py --zip /path/to/vps_full_report_latest.zip

It (read-only w.r.t. the engine) extracts a curated, ChatGPT-friendly subset — the bot
report.json/report.md (from the embedded light bundle), validation verdicts, git + env
proof, docker ps, and the durable runtime_metrics/*.json — into ``<repo>/vps_full_reports/
latest/`` (cleared + overwritten each time), writes a MANIFEST, and SKIPS large raw streams
(*.jsonl) and embedded *.zip to keep the folder lean. Stdlib only.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

_REDACTED = "<redacted>"
# API-key token shapes to scrub defensively even if not in the configured secret list.
_KEY_PATTERNS = [
    re.compile(r"\bsk-or-v1-[A-Za-z0-9_\-]{16,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b"),
    re.compile(r"\bxai-[A-Za-z0-9_\-]{16,}\b"),
    re.compile(r"\bgsk_[A-Za-z0-9_\-]{16,}\b"),
]


def _secret_values() -> list:
    """Configured secret VALUES from the cloud-agent env (longest first), so a report's
    embedded env/git data can never carry a real key into the committed repo."""
    names = set()
    for env in ("CLOUD_AGENT_ALL_SECRET_NAMES", "CLOUD_AGENT_INJECTED_SECRET_NAMES"):
        names.update(n.strip() for n in (os.getenv(env, "") or "").split(",") if n.strip())
    names.update({"OPENROUTER_API_KEY", "XAI_API_KEY", "GROK_API_KEY"})
    vals = {(os.getenv(n, "") or "").strip() for n in names}
    return sorted((v for v in vals if v and len(v) >= 4), key=len, reverse=True)


def redact_text(text: str, secrets: list) -> str:
    out = text
    for v in secrets:
        if v and v != _REDACTED:
            out = out.replace(v, _REDACTED)
    for pat in _KEY_PATTERNS:
        out = pat.sub(_REDACTED, out)
    return out


def _copy_redacted(src: Path, dst: Path, secrets: list) -> None:
    """Copy a text report file with secrets redacted (never commit secrets to the repo)."""
    try:
        txt = src.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        shutil.copy2(src, dst)            # non-text: copy as-is (we only select text files)
        return
    dst.write_text(redact_text(txt, secrets), encoding="utf-8")

MAX_FILE_BYTES = 3_000_000          # skip individual files larger than ~3 MB (raw streams)
DEST_REL = "vps_full_reports/latest"
# top-level full-report files worth committing (names as written by vps_generate_full_report.sh)
KEEP_TOPLEVEL = {
    "validation_full.txt", "validation_light_latest.txt", "git_commit.txt",
    "git_status.txt", "hermes_training_env_proof.txt", "docker_compose_ps.txt",
    "docker_compose_config_check.txt", "generate_full_report.txt", "latest_zip_listing.txt",
    "latest_zip_size.txt",
}
RENAME = {"validation_light_latest.txt": "validation_light.txt"}  # avoid gitignore name clash


def _find_repo_root(start: Path) -> Path:
    p = start.resolve()
    for cand in [p, *p.parents]:
        if (cand / ".git").exists():
            return cand
    return start.resolve()


def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    dest = dest.resolve()
    for m in zf.namelist():
        target = (dest / m).resolve()
        if not str(target).startswith(str(dest)):       # zip-slip guard
            continue
        if m.endswith("/"):
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(m) as src, open(target, "wb") as out:
            out.write(src.read())


def save(zip_path: Path, repo_root: Path) -> dict:
    dest = repo_root / DEST_REL
    if dest.exists():
        shutil.rmtree(dest)
    (dest / "runtime_metrics").mkdir(parents=True, exist_ok=True)
    secrets = _secret_values()
    captured, skipped = [], []

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        with zipfile.ZipFile(zip_path) as zf:
            _safe_extract(zf, tmp)
        # the zip has a single top-level vps_full_report_<ts>/ dir
        roots = [p for p in tmp.iterdir() if p.is_dir()] or [tmp]
        base = roots[0]

        # 1) curated top-level text files
        for name in KEEP_TOPLEVEL:
            f = base / name
            if f.is_file() and f.stat().st_size <= MAX_FILE_BYTES:
                _copy_redacted(f, dest / RENAME.get(name, name), secrets)
                captured.append(RENAME.get(name, name))

        # 2) runtime_metrics/*.json (skip giant files + non-json streams)
        rmsrc = base / "runtime_metrics"
        if rmsrc.is_dir():
            for f in sorted(rmsrc.iterdir()):
                if f.suffix == ".json" and f.stat().st_size <= MAX_FILE_BYTES:
                    _copy_redacted(f, dest / "runtime_metrics" / f.name, secrets)
                    captured.append(f"runtime_metrics/{f.name}")
                elif f.is_file():
                    skipped.append(f"runtime_metrics/{f.name} ({f.stat().st_size}B)")

        # 3) the bot report.json/report.md live inside the embedded light bundle zip
        light = base / "vps_light_report_latest.zip"
        if light.is_file():
            with tempfile.TemporaryDirectory() as ltd:
                lt = Path(ltd)
                try:
                    with zipfile.ZipFile(light) as lzf:
                        _safe_extract(lzf, lt)
                except zipfile.BadZipFile:
                    lt = None
                if lt is not None:
                    for want in ("report.json", "report.md", "final_validation.json",
                                 "validation_contract.json", "algorithmic_edge_audit.json"):
                        hits = sorted(lt.rglob(want))
                        if hits and hits[0].stat().st_size <= MAX_FILE_BYTES:
                            _copy_redacted(hits[0], dest / want, secrets)
                            captured.append(want)

    commit = ""
    gc = dest / "git_commit.txt"
    if gc.is_file():
        commit = gc.read_text(encoding="utf-8").strip()[:40]
    skipped_lines = [f"- {s}" for s in sorted(skipped)] or ["- none"]
    manifest = [
        "# VPS full report — extracted for ChatGPT inspection",
        f"generated_utc: {_dt.datetime.now(_dt.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        f"source_zip: {zip_path.name}",
        f"source_commit: {commit or 'unknown'}",
        f"files_captured: {len(captured)}",
        "", "## captured", *[f"- {c}" for c in sorted(captured)],
        "", "## skipped (too large / raw stream)", *skipped_lines,
    ]
    (dest / "MANIFEST.txt").write_text("\n".join(manifest) + "\n", encoding="utf-8")
    return {"dest": str(dest), "captured": len(captured), "skipped": len(skipped),
            "commit": commit}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Extract a VPS full-report zip into the repo "
                                             "vps_full_reports/latest/ for ChatGPT inspection.")
    ap.add_argument("--zip", required=True, help="path to the pulled vps_full_report zip")
    ap.add_argument("--repo-root", default=None, help="repo root (default: auto-detect .git)")
    args = ap.parse_args(argv)
    zip_path = Path(args.zip)
    if not zip_path.is_file():
        print(f"FATAL: zip not found: {zip_path}", file=sys.stderr)
        return 2
    repo_root = Path(args.repo_root) if args.repo_root else _find_repo_root(Path.cwd())
    res = save(zip_path, repo_root)
    print(f"saved {res['captured']} files -> {res['dest']} "
          f"(skipped {res['skipped']}; commit {res['commit'] or 'unknown'})")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
