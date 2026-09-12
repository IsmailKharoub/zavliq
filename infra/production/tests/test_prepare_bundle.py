"""Offline bundle provenance and command contracts. No Docker commands execute."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
from subprocess import CompletedProcess
import tarfile
import tempfile
import unittest
import stat
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('production_bundle', Path(__file__).resolve().parents[1] / 'prepare_bundle.py')
bundle = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bundle)
REVISION = 'a' * 40


def archive(path, files, *, gzip=True):
    with tarfile.open(path, 'w:gz' if gzip else 'w') as output:
        for name, body in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(body)
            output.addfile(member, io.BytesIO(body))


def encoded(value):
    return json.dumps(value, sort_keys=True).encode()


class Fixture:
    def __init__(self, root, extra_source=None):
        self.root, self.stage = root, root / 'stage'
        self.stage.mkdir()
        self.native = root / 'native-runtime.tar.gz'
        self.output = root / 'output'
        binary = b'\x7fELF\x02\x01' + b'\0' * 12 + b'\x3e\x00' + b'fixture native binary'
        archive(self.native, {'zavliq': binary, 'LICENSE': b'license', 'README.md': b'readme'})
        runtime = {'repository': 'IsmailKharoub/zavliq', 'workflow': '.github/workflows/release.yml', 'run_id': 123,
                   'head_sha': REVISION, 'target': 'x86_64-unknown-linux-gnu',
                   'archive_sha256': bundle.sha256(self.native), 'binary_sha256': hashlib.sha256(binary).hexdigest()}
        files = {'infra/docker/web.Dockerfile': b'FROM node:24.21.0-bookworm-slim AS build\nARG VITE_ECHO_USER_ID\nRUN corepack enable\nFROM caddy:2.11.4-alpine\nCOPY --from=build /app/dist /srv\n',
                 'infra/docker/web.Dockerfile.dockerignore': b'**/.local\n', 'infra/compose.yaml': b'services: {}\n',
                 'infra/compose.production.yaml': b'services: {}\n', 'apps/web/example.tsx': b'archived application'}
        files.update(extra_source or {})
        archive(self.stage / 'source.tar.gz', files)
        for name, body in files.items():
            if name.startswith('infra/'):
                path = self.stage / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(body)
        self.configs, self.images = {}, {}
        for name in sorted(bundle.SERVICES):
            ref = 'postgres:17.11-alpine' if name == 'postgres' else f'zavliq-{"web" if name == "gateway" else name}:{REVISION}'
            labels = {'fixture.service': name}
            if name == 'echo':
                labels.update({'org.opencontainers.image.revision': REVISION, 'com.zavliq.native.sha256': runtime['binary_sha256']})
            config = encoded({'architecture': 'amd64', 'os': 'linux', 'config': {'Labels': labels}})
            image_id = 'sha256:' + hashlib.sha256(config).hexdigest()
            self.configs[image_id] = config
            self.images[name] = {'ref': ref, 'id': image_id}
        self.docker_archive(self.stage / 'images.tar.gz', self.images)
        self.manifest = {'revision': REVISION, 'architecture': 'linux/amd64', 'web_echo_user_id': '@echo:localhost',
                         'source_archive_sha256': bundle.sha256(self.stage / 'source.tar.gz'),
                         'images_archive_sha256': bundle.sha256(self.stage / 'images.tar.gz'),
                         'images': self.images, 'native_runtime': runtime, 'postgres': self.images['postgres'],
                         'infra_hashes': bundle.tree_hashes(self.stage)}
        self.write_manifest()
        self.bases_path = root / 'bases.json'
        self.bases = {'schema': 'zavliq.public-web-bases.v1', 'architecture': 'linux/amd64', 'images': {
            name: {'ref': ref, 'id': 'sha256:' + character * 64,
                   'digest_ref': 'docker.io/library/' + name + '@sha256:' + character * 64}
            for (name, ref), character in zip(bundle.BASE_REFS.items(), ['b', 'c'])}}
        self.write_bases()

    def write_manifest(self):
        (self.stage / 'manifest.json').write_bytes(encoded(self.manifest))
        self.manifest_hash = bundle.sha256(self.stage / 'manifest.json')

    def write_bases(self):
        self.bases_path.write_bytes(encoded(self.bases))
        self.bases_hash = bundle.sha256(self.bases_path)

    def docker_archive(self, path, images, *, compressed=True):
        files, manifest = {}, []
        for image in images.values():
            name = image['id'][7:] + '.json'
            files[name] = self.configs[image['id']]
            manifest.append({'Config': name, 'RepoTags': [image['ref']], 'Layers': []})
        files['manifest.json'] = encoded(manifest)
        archive(path, files, gzip=compressed)

    def verify(self):
        source = self.root / 'extracted'
        source.mkdir()
        return bundle.verify_inputs(self.stage, self.manifest_hash, self.native, self.bases_path, self.bases_hash, source)

    def prepare(self):
        return bundle.prepare(self.stage, self.manifest_hash, self.native, self.bases_path, self.bases_hash, self.output)


class DockerRig:
    def __init__(self, fixture):
        self.fixture, self.calls = fixture, []
        self.architecture, self.driver, self.context = 'linux/x86_64', 'docker', 'default'
        self.cached = {item['ref']: item['id'] for item in fixture.images.values()}
        self.bad_base, self.web = False, None

    def inspect(self, ref):
        for base in self.fixture.bases['images'].values():
            if ref == base['digest_ref']:
                return {'Id': 'sha256:' + 'd' * 64 if self.bad_base else base['id'], 'Architecture': 'amd64', 'Os': 'linux'}
        image_id = self.cached[ref]
        config = json.loads(self.fixture.configs[image_id])
        return {'Id': image_id, 'Architecture': config['architecture'], 'Os': config['os'], 'Config': config['config']}

    def run(self, command, **kwargs):
        self.calls.append((command, kwargs))
        stdout = ''
        if command == ['docker', 'context', 'show']:
            stdout = self.context
        elif command[:2] == ['docker', 'info']:
            stdout = self.architecture
        elif command[:3] == ['docker', 'buildx', 'inspect']:
            stdout = 'Name: default\nDriver: ' + self.driver + '\n'
        elif command[:3] == ['docker', 'image', 'inspect']:
            stdout = json.dumps(self.inspect(command[-1]))
        elif command[:3] == ['docker', 'image', 'ls']:
            ref = command[-1].removeprefix('reference=')
            stdout = self.cached.get(ref, '')
        elif command[:2] == ['docker', 'load']:
            pass
        elif command[:3] == ['docker', 'buildx', 'build']:
            source = kwargs['cwd']
            self.assert_archived_source(source)
            tag = command[command.index('-t') + 1]
            labels = dict(command[index + 1].split('=', 1) for index, item in enumerate(command) if item == '--label')
            config = encoded({'architecture': 'amd64', 'os': 'linux', 'config': {'Labels': labels}})
            image_id = 'sha256:' + hashlib.sha256(config).hexdigest()
            self.fixture.configs[image_id] = config
            self.cached[tag] = image_id
            self.web = {'ref': tag, 'id': image_id}
        elif command[:2] == ['docker', 'save']:
            images = {str(index): {'ref': ref, 'id': self.cached[ref]} for index, ref in enumerate(command[4:])}
            self.fixture.docker_archive(Path(command[3]), images, compressed=False)
        else:
            raise AssertionError('Unexpected command: ' + repr(command))
        return CompletedProcess(command, 0, stdout, '')

    def assert_archived_source(self, source):
        assert (source / 'apps/web/example.tsx').read_bytes() == b'archived application'
        original = (source / 'infra/docker/web.Dockerfile').read_text()
        assert original.startswith('FROM node:24.21.0-bookworm-slim AS build')
        pinned = (source / 'infra/docker/public-web.Dockerfile').read_text()
        for item in self.fixture.bases['images'].values():
            assert item['digest_ref'] in pinned
        assert (source / 'infra/docker/public-web.Dockerfile.dockerignore').read_bytes() == b'**/.local\n'


class ProductionBundleTests(unittest.TestCase):
    def test_completed_bundle_preserves_backend_native_source_and_infra_only_rebuilds_public_web(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            rig = DockerRig(fixture)
            with patch.object(bundle, 'checked', side_effect=rig.run):
                result = fixture.prepare()
            manifest = json.loads((fixture.output / 'production-manifest.json').read_text())
            self.assertEqual(manifest['schema'], bundle.SCHEMA)
            self.assertFalse(manifest['activation_supported'])
            self.assertEqual(manifest['native_runtime'], fixture.manifest['native_runtime'])
            self.assertEqual(manifest['reviewed_stage']['manifest_sha256'], fixture.manifest_hash)
            self.assertEqual(manifest['images_archive_sha256'], bundle.sha256(fixture.output / 'images.tar.gz'))
            self.assertEqual(manifest['infra_hashes'], bundle.tree_hashes(fixture.output))
            self.assertEqual(bundle.sha256(fixture.output / 'source.tar.gz'), fixture.manifest['source_archive_sha256'])
            self.assertEqual(bundle.sha256(fixture.output / 'native-runtime.tar.gz'), fixture.manifest['native_runtime']['archive_sha256'])
            self.assertFalse((fixture.output / 'manifest.json').exists())
            for name in bundle.SERVICES - {'gateway'}:
                self.assertEqual(manifest['images'][name], fixture.images[name])
            self.assertNotEqual(manifest['images']['gateway'], fixture.images['gateway'])
            self.assertRegex(result['public_web']['ref'], '^zavliq-web:' + REVISION + '-public-[0-9a-f]{16}$')
            build_calls = [args for args, _ in rig.calls if args[:3] == ['docker', 'buildx', 'build']]
            self.assertEqual(len(build_calls), 1)
            self.assertEqual(build_calls[0], manifest['public_web']['command'])
            self.assertIn('VITE_ECHO_USER_ID=@echo:zavliq.com', build_calls[0])
            self.assertIn('--pull=false', build_calls[0])
            self.assertEqual(build_calls[0][build_calls[0].index('--builder') + 1], 'default')
            self.assertFalse(any('cargo' in args or 'pull' in args or 'compose' in args or 'run' in args for args, _ in rig.calls))
            overlay = (fixture.output / 'compose.images.yaml').read_text()
            self.assertEqual(overlay.count('build: !reset null'), 9)
            self.assertEqual(overlay.count('pull_policy: never'), 9)
            self.assertNotIn(fixture.images['gateway']['id'], overlay)
            for name in bundle.SERVICES | set(bundle.HELPERS):
                self.assertIn('  ' + name + ':\n', overlay)

    def test_changed_reviewed_manifest_or_archive_fails_before_any_docker_call(self):
        for name in ['manifest.json', 'source.tar.gz', 'images.tar.gz', 'native-runtime.tar.gz', 'bases.json']:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                fixture = Fixture(Path(temporary))
                path = fixture.native if name == 'native-runtime.tar.gz' else fixture.bases_path if name == 'bases.json' else fixture.stage / name
                path.write_bytes(path.read_bytes() + b'changed')
                with patch.object(bundle, 'checked') as run, self.assertRaisesRegex(ValueError, 'REVIEWED_INPUT_HASH_MISMATCH'):
                    fixture.prepare()
                run.assert_not_called()
                self.assertFalse(fixture.output.exists())

    def test_changed_native_binary_with_rehashed_archive_still_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            binary = b'\x7fELF\x02\x01' + b'\0' * 12 + b'\x3e\x00' + b'different native'
            archive(fixture.native, {'zavliq': binary, 'README.md': b'x', 'LICENSE': b'x'})
            fixture.manifest['native_runtime']['archive_sha256'] = bundle.sha256(fixture.native)
            fixture.write_manifest()
            with self.assertRaisesRegex(ValueError, 'NATIVE_BINARY_HASH_MISMATCH'):
                fixture.verify()

    def test_existing_output_is_never_reused(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            fixture.output.mkdir()
            marker = fixture.output / 'existing-evidence'
            marker.write_text('preserve')
            with patch.object(bundle, 'checked') as run, self.assertRaisesRegex(ValueError, 'FRESH_OUTPUT_DIRECTORY_REQUIRED'):
                fixture.prepare()
            run.assert_not_called()
            self.assertEqual(marker.read_text(), 'preserve')

    def test_wrong_source_origin_runtime_workflow_or_missing_backend_is_refused(self):
        mutations = [lambda value: value.update(web_echo_user_id='@echo:zavliq.com'),
                     lambda value: value['native_runtime'].update(head_sha='b' * 40),
                     lambda value: value['native_runtime'].update(repository='other/repository'),
                     lambda value: value['native_runtime'].update(workflow='arbitrary.yml'),
                     lambda value: value['images'].pop('echo')]
        for change in mutations:
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                fixture = Fixture(Path(temporary))
                change(fixture.manifest)
                fixture.write_manifest()
                with patch.object(bundle, 'checked') as run, self.assertRaises(ValueError):
                    fixture.prepare()
                run.assert_not_called()

    def test_infra_must_be_complete_and_match_archived_source_not_only_its_own_manifest(self):
        for mode in ['missing', 'extra', 'modified', 'modified-and-rehashed']:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                fixture = Fixture(Path(temporary))
                path = fixture.stage / 'infra/compose.yaml'
                if mode == 'missing': path.unlink()
                elif mode == 'extra': (fixture.stage / 'infra/extra').write_text('extra')
                else: path.write_text('modified')
                if mode == 'modified-and-rehashed':
                    fixture.manifest['infra_hashes'] = bundle.tree_hashes(fixture.stage)
                    fixture.write_manifest()
                with self.assertRaisesRegex(ValueError, 'EXACT_ARCHIVED_INFRA_REQUIRED'):
                    fixture.verify()

    def test_docker_archive_tags_configs_architecture_and_echo_labels_are_verified(self):
        for mode in ['tag', 'config', 'architecture', 'echo-label']:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                fixture = Fixture(Path(temporary))
                if mode == 'tag':
                    fixture.images['gateway']['ref'] = 'unexpected:tag'
                elif mode == 'config':
                    fixture.configs[fixture.images['gateway']['id']] += b' '
                else:
                    service = 'echo' if mode == 'echo-label' else 'gateway'
                    config = json.loads(fixture.configs[fixture.images[service]['id']])
                    if mode == 'echo-label': config['config']['Labels'].pop('com.zavliq.native.sha256')
                    else: config['architecture'] = 'arm64'
                    data = encoded(config)
                    image_id = 'sha256:' + hashlib.sha256(data).hexdigest()
                    fixture.configs[image_id] = data
                    fixture.images[service]['id'] = image_id
                fixture.docker_archive(fixture.stage / 'images.tar.gz', fixture.images)
                fixture.manifest['images_archive_sha256'] = bundle.sha256(fixture.stage / 'images.tar.gz')
                fixture.write_manifest()
                with self.assertRaises(ValueError): fixture.verify()

    def test_non_native_or_remote_builder_missing_base_and_tag_collisions_block_mutations(self):
        for mode in ['context', 'arm64', 'remote', 'base', 'stage-tag', 'public-tag']:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                fixture = Fixture(Path(temporary))
                rig = DockerRig(fixture)
                if mode == 'context': rig.context = 'other-builder'
                elif mode == 'arm64': rig.architecture = 'linux/aarch64'
                elif mode == 'remote': rig.driver = 'docker-container'
                elif mode == 'base': rig.bad_base = True
                elif mode == 'stage-tag': rig.cached[fixture.images['control']['ref']] = 'sha256:' + 'f' * 64
                real_run = rig.run

                def run(command, **kwargs):
                    if mode == 'public-tag' and command[:3] == ['docker', 'image', 'ls'] and '-public-' in command[-1]:
                        return CompletedProcess(command, 0, 'sha256:' + 'f' * 64, '')
                    return real_run(command, **kwargs)

                with patch.object(bundle, 'checked', side_effect=run), self.assertRaises(ValueError):
                    fixture.prepare()
                self.assertFalse(any(args[:2] in [['docker', 'load'], ['docker', 'save']] or args[:3] == ['docker', 'buildx', 'build'] for args, _ in rig.calls))
                self.assertFalse(fixture.output.exists())

    def test_unpinned_base_inputs_and_dockerfile_drift_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            fixture.bases['images']['node']['digest_ref'] = 'node:latest'
            fixture.write_bases()
            with self.assertRaisesRegex(ValueError, 'EXACT_WEB_BASE_PINS_REQUIRED'):
                fixture.verify()
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            source = Path(temporary) / 'source'
            bundle.extract_source(fixture.stage / 'source.tar.gz', source)
            path = source / 'infra/docker/web.Dockerfile'
            path.write_text(path.read_text().replace('node:24.21.0-bookworm-slim', 'node:latest'))
            with self.assertRaisesRegex(ValueError, 'REVIEWED_WEB_DOCKERFILE_REQUIRED'):
                bundle.pinned_dockerfile(source, fixture.bases)

    def test_build_failure_leaves_no_complete_manifest_or_activation(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            rig = DockerRig(fixture)

            def run(command, **kwargs):
                if command[:3] == ['docker', 'buildx', 'build']: raise RuntimeError('fixture failure')
                return rig.run(command, **kwargs)

            with patch.object(bundle, 'checked', side_effect=run), self.assertRaises(RuntimeError): fixture.prepare()
            self.assertFalse((fixture.output / 'production-manifest.json').exists())

    def test_inputs_changed_during_build_do_not_produce_a_completed_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            rig = DockerRig(fixture)

            def run(command, **kwargs):
                result = rig.run(command, **kwargs)
                if command[:3] == ['docker', 'buildx', 'build']:
                    fixture.native.write_bytes(fixture.native.read_bytes() + b'changed during build')
                return result

            with patch.object(bundle, 'checked', side_effect=run), self.assertRaisesRegex(ValueError, 'REVIEWED_INPUT_HASH_MISMATCH'):
                fixture.prepare()
            self.assertFalse((fixture.output / 'production-manifest.json').exists())

    def test_source_archive_refuses_links_traversal_private_paths_and_duplicates(self):
        for name, kind in [('../outside', tarfile.REGTYPE), ('link', tarfile.SYMTYPE), ('.local/private', tarfile.REGTYPE)]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / 'source.tar.gz'
                with tarfile.open(path, 'w:gz') as output:
                    member = tarfile.TarInfo(name)
                    member.type, member.linkname = kind, '/outside'
                    output.addfile(member)
                with self.assertRaises(ValueError): bundle.extract_source(path, Path(temporary) / 'source')
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'source.tar.gz'
            with tarfile.open(path, 'w:gz') as output:
                output.addfile(tarfile.TarInfo('duplicate'))
                output.addfile(tarfile.TarInfo('duplicate'))
            with self.assertRaisesRegex(ValueError, 'DUPLICATE_ARCHIVE_MEMBER'):
                bundle.extract_source(path, Path(temporary) / 'source')

    def test_restrictive_umask_keeps_outer_private_but_public_source_traversable_and_executables_intact(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path, destination = root / 'source.tar.gz', root / 'private-source'
            with tarfile.open(path, 'w:gz') as output:
                directory = tarfile.TarInfo('apps')
                directory.type, directory.mode = tarfile.DIRTYPE, 0o755
                output.addfile(directory)
                for name, mode in [('apps/web/file.txt', 0o644), ('infra/scripts/start.sh', 0o755)]:
                    member = tarfile.TarInfo(name)
                    member.mode, member.size = mode, 1
                    output.addfile(member, io.BytesIO(b'x'))
            previous = os.umask(0o077)
            try:
                destination.mkdir(mode=0o700)
                bundle.extract_source(path, destination)
            finally:
                os.umask(previous)
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            for name in ['apps', 'apps/web', 'infra', 'infra/scripts']:
                with self.subTest(directory=name):
                    self.assertEqual(stat.S_IMODE((destination / name).stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE((destination / 'apps/web/file.txt').stat().st_mode), 0o644)
            self.assertEqual(stat.S_IMODE((destination / 'infra/scripts/start.sh').stat().st_mode), 0o755)


if __name__ == '__main__':
    unittest.main()
