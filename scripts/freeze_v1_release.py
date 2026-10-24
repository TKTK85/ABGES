"""Create an immutable, hash-manifested ABGES source snapshot outside workspace."""
from hashlib import sha256
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile
import subprocess


ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "releases" / "ABGES_V1.0"
ARCHIVE = DEST / "ABGES_V1.0_source.zip"
MANIFEST = DEST / "ABGES_V1.0_manifest.sha256"
EXCLUDED_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", "workspace",
                 "releases", "node_modules", "venv", "dist", "build"}


def source_files():
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or any(part in EXCLUDED_DIRS for part in path.relative_to(ROOT).parts):
            continue
        if path.suffix.lower() in {".log", ".pyc", ".pyo"} or path.name == ".coverage":
            continue
        yield path


def main():
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    if version != "1.0.0":
        raise SystemExit("VERSION must be 1.0.0 before freezing; found %s" % version)
    DEST.mkdir(parents=True, exist_ok=True)
    files = list(source_files())
    rows = []
    for path in files:
        digest = sha256(path.read_bytes()).hexdigest()
        rows.append("%s  %s" % (digest, path.relative_to(ROOT).as_posix()))
    manifest_text = "\n".join(rows) + "\n"
    (DEST / "MANIFEST.sha256").unlink(missing_ok=True)
    MANIFEST.write_text(manifest_text, encoding="utf-8")
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
            stderr=subprocess.DEVNULL).strip()
    except Exception:
        commit = "unavailable"
    (DEST / "RELEASE_INFO.txt").write_text(
        "ABGES V1.0 / semantic version 1.0.0\n"
        "Source file count: %d\nBase Git commit: %s\n"
        "Working-tree source state is bound by MANIFEST.sha256.\n"
        "workspace/, .git/, virtual environments, caches and logs are excluded.\n"
        % (len(files), commit), encoding="utf-8")
    with ZipFile(ARCHIVE, "w", compression=ZIP_DEFLATED, compresslevel=6) as bundle:
        for path in files:
            bundle.write(path, path.relative_to(ROOT).as_posix())
        bundle.writestr("RELEASE_MANIFEST.sha256", manifest_text)
        bundle.writestr("RELEASE_INFO.txt", (DEST / "RELEASE_INFO.txt").read_text(encoding="utf-8"))
    archive_hash = sha256(ARCHIVE.read_bytes()).hexdigest()
    (DEST / "ABGES_V1.0_archive.sha256").write_text(
        "%s  %s\n" % (archive_hash, ARCHIVE.name), encoding="utf-8")
    print("version=%s files=%d archive=%s sha256=%s" %
          (version, len(files), ARCHIVE, archive_hash))


if __name__ == "__main__":
    main()
