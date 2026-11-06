"""Regression tests; run locally or inside the Dagger test container."""
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from mirror.fetch import MirrorError, extract_archive, safe_name, validate_url
from mirror.apply import apply


class MirrorTests(unittest.TestCase):
    def make_zip(self, entries):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        archive = Path(folder.name) / "repo.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            for name, data in entries.items():
                zf.writestr(name, data)
        return Path(folder.name), archive

    def test_path_traversal(self):
        for value in ["../escape", "/abs", "a/../../b", ".git/config", "a\\b"]:
            with self.subTest(value=value), self.assertRaises(MirrorError):
                safe_name(value)

    def test_source_allowlist(self):
        validate_url("https://anonymous.4open.science/api/repo/CoqProver-Code-57BB/zip")
        with self.assertRaises(MirrorError):
            validate_url("https://example.com/archive.zip")

    def test_extract_files(self):
        folder, archive = self.make_zip({"README.md": b"hello", "src/main.py": b"\x00\xff"})
        metrics = extract_archive(archive, folder / "out")
        self.assertEqual(metrics["files"], 2)
        self.assertEqual((folder / "out/src/main.py").read_bytes(), b"\x00\xff")

    def test_no_empty_snapshot(self):
        folder, archive = self.make_zip({})
        with self.assertRaises(MirrorError):
            extract_archive(archive, folder / "out")

    def test_reject_symlink(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        archive = Path(folder.name) / "source.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            info = zipfile.ZipInfo("bad")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(info, b"/etc/passwd")
        with self.assertRaises(MirrorError):
            extract_archive(archive, Path(folder.name) / "out")

    def test_sync_only_upstream(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        stage = root / ".mirror-stage"
        (stage / "source").mkdir(parents=True)
        (stage / "source/hello").write_text("new")
        (stage / "manifest.json").write_text('{"files":1}')
        (root / "upstream").mkdir()
        (root / "upstream/stale").write_text("old")
        (root / "mirror").mkdir()
        (root / "mirror/control").write_text("keep")
        apply(stage, root / "upstream")
        self.assertFalse((root / "upstream/stale").exists())
        self.assertEqual((root / "upstream/hello").read_text(), "new")
        self.assertEqual((root / "mirror/control").read_text(), "keep")


if __name__ == "__main__":
    unittest.main()
