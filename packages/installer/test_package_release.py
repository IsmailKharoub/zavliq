import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("package_release", Path(__file__).with_name("package-release.py"))
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleasePackagingTests(unittest.TestCase):
    def test_new_version_preserves_public_dependency_lock(self):
        original = json.loads(release.LOCK.read_text())
        dependencies = dict(original["packages"]["node_modules/@zavliq/mcp"]["dependencies"])
        dependencies["@zavliq/client"] = "1.2.3"
        with tempfile.TemporaryDirectory() as temporary:
            vendor = Path(temporary)
            for name in ("client", "mcp"):
                (vendor / f"zavliq-{name}-1.2.3.tgz").write_bytes(b"local package fixture")
            package, lock = release.make_lock("1.2.3", vendor, {"mcp": {"dependencies": dependencies}})
        self.assertEqual(package["dependencies"]["@zavliq/client"], "file:vendor/zavliq-client-1.2.3.tgz")
        for name, entry in original["packages"].items():
            if name not in ("", "node_modules/@zavliq/client", "node_modules/@zavliq/mcp"):
                self.assertEqual(lock["packages"][name], entry)
        self.assertTrue(lock["packages"]["node_modules/@zavliq/mcp"]["integrity"].startswith("sha512-"))

    def test_changed_dependency_requires_explicit_lock_refresh(self):
        original = json.loads(release.LOCK.read_text())
        dependencies = dict(original["packages"]["node_modules/@zavliq/mcp"]["dependencies"])
        dependencies["zod"] = "^99.0.0"
        with self.assertRaisesRegex(ValueError, "refresh"):
            release.make_lock("0.1.0", Path("/unused"), {"mcp": {"dependencies": dependencies}})

    def test_portable_archive_is_stable_across_filesystem_timestamps(self):
        import os
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "bundle"
            source.mkdir()
            item = source / "package.json"
            item.write_text('{"private":true}\n')
            first, second = base / "first.tar.gz", base / "second.tar.gz"
            release.archive(source, first, 1700000000)
            os.utime(item, (1800000000, 1800000000))
            release.archive(source, second, 1700000000)
            self.assertEqual(hashlib.sha256(first.read_bytes()).digest(), hashlib.sha256(second.read_bytes()).digest())


if __name__ == "__main__":
    unittest.main()
