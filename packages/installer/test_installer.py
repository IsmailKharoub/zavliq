"""Installer contract tests use local synthetic release files; no network or user installation."""
import hashlib
import io
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

SCRIPT = Path(__file__).with_name('install.sh').resolve()
ARCHIVE = 'zavliq-v0.1.0-x86_64-unknown-linux-gnu.tar.gz'

class Installer(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='zavliq-installer-')
        self.root = Path(self.temp.name)
        tools = self.root / 'tools'; tools.mkdir()
        assets = self.root / 'assets'; assets.mkdir()
        for name, source in {
            'uname': '#!/bin/sh\ncase "$1" in -s) echo Linux;; -m) echo x86_64;; esac\n',
            'getconf': '#!/bin/sh\necho "glibc 2.35"\n',
            'curl': '#!/usr/bin/env python3\nimport os,sys,shutil\na=sys.argv[1:];u=next(s for s in a if s.startswith("https://"));out=a[a.index("-o")+1];shutil.copyfile(os.path.join(os.environ["FIXTURE_ASSETS"],u.rsplit("/",1)[1]),out)\n',
        }.items():
            file=tools/name;file.write_text(source);file.chmod(0o755)
        self.binary=b'#!/bin/sh\nprintf "synthetic executable\\n"\n'
        with tarfile.open(assets / ARCHIVE, 'w:gz') as archive:
            item=tarfile.TarInfo('zavliq');item.size=len(self.binary);item.mode=0o755
            archive.addfile(item,io.BytesIO(self.binary))
        self.hash=hashlib.sha256((assets/ARCHIVE).read_bytes()).hexdigest()
        (assets/'SHA256SUMS').write_text(f'{self.hash}  {ARCHIVE}\n')
        self.env={**os.environ,'PATH':str(tools)+os.pathsep+os.environ['PATH'],'FIXTURE_ASSETS':str(assets)}
        self.destination=self.root/'installed'

    def tearDown(self):self.temp.cleanup()
    def run_installer(self,*args):
        return subprocess.run(['sh',str(SCRIPT),'--install-dir',str(self.destination),*args],env=self.env,capture_output=True,text=True,timeout=10)
    def test_explicit_version_and_verified_install(self):
        self.assertNotEqual(self.run_installer().returncode,0)
        result=self.run_installer('--version','v0.1.0')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual((self.destination/'zavliq').read_bytes(),self.binary)
        self.assertTrue((self.destination/'zavliq').stat().st_mode & 0o111)
        self.assertNotEqual(self.run_installer('--version','v0.1.0').returncode,0)
        self.assertEqual(self.run_installer('--version','v0.1.0','--force').returncode,0)
    def test_checksum_mismatch_installs_nothing(self):
        (self.root/'assets'/'SHA256SUMS').write_text(f'{"0"*64}  {ARCHIVE}\n')
        result=self.run_installer('--version','v0.1.0')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('Checksum mismatch',result.stderr)
        self.assertFalse(self.destination.exists())
    def test_missing_manifest_entry_installs_nothing(self):
        (self.root/'assets'/'SHA256SUMS').write_text(f'{self.hash}  other.tar.gz\n')
        self.assertNotEqual(self.run_installer('--version','v0.1.0').returncode,0)
        self.assertFalse(self.destination.exists())
    def test_reviewed_manifest_pin_accepts_exact_release(self):
        pin=hashlib.sha256((self.root/'assets/SHA256SUMS').read_bytes()).hexdigest()
        result=self.run_installer('--version','v0.1.0','--sha256sums-sha256',pin)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual((self.destination/'zavliq').read_bytes(),self.binary)
    def test_reviewed_manifest_pin_rejects_consistent_changed_release(self):
        assets=self.root/'assets'
        pin=hashlib.sha256((assets/'SHA256SUMS').read_bytes()).hexdigest()
        changed=b'#!/bin/sh\nprintf "different synthetic executable\\n"\n'
        with tarfile.open(assets/ARCHIVE,'w:gz') as archive:
            item=tarfile.TarInfo('zavliq');item.size=len(changed);item.mode=0o755
            archive.addfile(item,io.BytesIO(changed))
        checksum=hashlib.sha256((assets/ARCHIVE).read_bytes()).hexdigest()
        (assets/'SHA256SUMS').write_text(f'{checksum}  {ARCHIVE}\n')
        result=self.run_installer('--version','v0.1.0','--sha256sums-sha256',pin)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('Manifest checksum mismatch',result.stderr)
        self.assertFalse(self.destination.exists())

if __name__=='__main__':unittest.main()
