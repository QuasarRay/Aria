"""Download and validate an Anonymous GitHub ZIP snapshot (not Git history)."""
from __future__ import annotations
import argparse
import hashlib
import json
import shutil
import stat
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

SOURCE_URL = "https://anonymous.4open.science/api/repo/CoqProver-Code-57BB/zip"
MAX_ZIP_BYTES = 350 * 1024 * 1024
MAX_TOTAL_BYTES = 900 * 1024 * 1024
MAX_FILE_BYTES = 95 * 1024 * 1024
MAX_FILES = 50000


class MirrorError(RuntimeError):
    pass


def validate_url(url: str) -> None:
    p = urlsplit(url)
    if (p.scheme != "https" or p.hostname != "anonymous.4open.science"
            or p.path != "/api/repo/CoqProver-Code-57BB/zip"
            or p.username or p.password or p.port or p.query or p.fragment):
        raise MirrorError("Unrecognized source URL; refusing arbitrary endpoint")


def download_archive(url: str, target: Path, attempts: int = 5) -> str:
    validate_url(url)
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "QuasarRay-Aria-source-snapshot/1.0",
                "Cache-Control": "no-cache",
                "Accept": "application/zip,application/octet-stream",
            })
            digest = hashlib.sha256()
            count = 0
            with urllib.request.urlopen(req, timeout=90) as response, target.open("wb") as out:
                if response.status != 200:
                    raise MirrorError(f"Archive endpoint returned HTTP {response.status}")
                while chunk := response.read(1024 * 1024):
                    count += len(chunk)
                    if count > MAX_ZIP_BYTES:
                        raise MirrorError("ZIP exceeds configured size limit")
                    digest.update(chunk)
                    out.write(chunk)
            if not zipfile.is_zipfile(target):
                raise MirrorError("Upstream response is not ZIP; refusing replacement")
            print(f"Downloaded {count:,} bytes; SHA256={digest.hexdigest()}", flush=True)
            return digest.hexdigest()
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 401, 403, 404, 410):
                raise MirrorError(f"Source unavailable (HTTP {exc.code}); no deletion") from exc
            last_error = exc
        except (urllib.error.URLError, TimeoutError, OSError, zipfile.BadZipFile) as exc:
            last_error = exc
        if attempt < attempts:
            pause = min(2 ** (attempt - 1), 16)
            print(f"Fetch attempt {attempt}/{attempts} failed: {last_error}; retry in {pause}s", flush=True)
            time.sleep(pause)
    raise MirrorError(f"Source could not be fetched: {last_error}")


def safe_name(raw: str) -> PurePosixPath:
    if not raw or "\\" in raw or "\x00" in raw or raw.startswith("/"):
        raise MirrorError(f"Unsafe ZIP path: {raw!r}")
    parts = raw.rstrip("/").split("/")
    if any(p in ("", ".", "..", ".git") for p in parts):
        raise MirrorError(f"Unsafe ZIP path: {raw!r}")
    return PurePosixPath(*parts)


def extract_archive(archive: Path, output: Path) -> dict[str, object]:
    output.mkdir(parents=True, exist_ok=False)
    seen: set[str] = set()
    count = 0
    total = 0
    digest = hashlib.sha256()
    with zipfile.ZipFile(archive) as zf:
        entries = zf.infolist()
        if len(entries) > MAX_FILES:
            raise MirrorError("Too many ZIP entries")
        for entry in entries:
            name = safe_name(entry.filename).as_posix()
            if entry.is_dir():
                continue
            mode = (entry.external_attr >> 16) & 0xFFFF
            if stat.S_IFMT(mode) == stat.S_IFLNK:
                raise MirrorError(f"Symlink is not permitted: {name}")
            if name in seen:
                raise MirrorError(f"Duplicate path: {name}")
            seen.add(name)
            if entry.file_size > MAX_FILE_BYTES:
                raise MirrorError(f"File exceeds GitHub limit: {name}")
            total += entry.file_size
            if total > MAX_TOTAL_BYTES:
                raise MirrorError("Uncompressed content exceeds limit")
            target = output / name
            target.parent.mkdir(parents=True, exist_ok=True)
            file_hash = hashlib.sha256()
            written = 0
            with zf.open(entry) as src, target.open("wb") as dst:
                while chunk := src.read(1024 * 1024):
                    written += len(chunk)
                    if written > MAX_FILE_BYTES:
                        raise MirrorError(f"File exceeds size limit: {name}")
                    dst.write(chunk)
                    file_hash.update(chunk)
            if written != entry.file_size:
                raise MirrorError(f"Incorrect uncompressed size: {name}")
            if mode & 0o111:
                target.chmod(0o755)
            digest.update(name.encode("utf-8") + b"\0" + file_hash.digest())
            count += 1
    if count == 0:
        raise MirrorError("Refusing empty upstream snapshot")
    return {"files": count, "uncompressed_bytes": total, "tree_digest": digest.hexdigest()}


def run(url: str, output: Path) -> None:
    validate_url(url)
    if output.exists():
        raise MirrorError(f"Output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        archive = root / "source.zip"
        sha = download_archive(url, archive)
        extracted = root / "source"
        metrics = extract_archive(archive, extracted)
        prepared = root / "export"
        prepared.mkdir()
        shutil.move(str(extracted), str(prepared / "source"))
        manifest = {"source_url": url, "archive_sha256": sha, **metrics}
        (prepared / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        shutil.move(str(prepared), str(output))
        print(json.dumps(manifest, indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=SOURCE_URL)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    run(args.url, args.output)
