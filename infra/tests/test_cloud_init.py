from pathlib import Path
import unittest


script = (Path(__file__).parents[1] / 'scripts/cloud-init.sh').read_text()
verification = script.split("python3 -B - <<'PY_DOCKER_VERIFY'\n", 1)[1].split('\nPY_DOCKER_VERIFY', 1)[0]
namespace = {'__name__': 'cloud_init_verification_test'}
exec(compile(verification, 'cloud-init.sh:docker-verification', 'exec'), namespace)
validate = namespace['validate_docker_host']


class CloudInitDockerValidation(unittest.TestCase):
    def setUp(self):
        self.info = {'OSType': 'linux', 'Architecture': 'x86_64',
                     'DriverStatus': [['driver-type', 'io.containerd.snapshotter.v1']]}

    def test_observed_ubuntu_compose_package_version(self):
        # Captured on the new production host after installing the pinned packages.
        validate(self.info, '29.1.3', '2.40.3+ds1-0ubuntu1~24.04.1')

    def test_reviewed_upstream_version(self):
        validate(self.info, '29.1.3', 'v2.40.3')

    def test_other_versions_and_unknown_suffixes_are_refused(self):
        for engine, compose in [('29.1.2', '2.40.3'), ('29.1.3', '2.40.4'),
                                ('29.1.3', '2.40.3+unreviewed'),
                                ('29.1.3', '2.40.3+ds1-0ubuntu1~24.04.2')]:
            with self.subTest(engine=engine, compose=compose), self.assertRaises(ValueError):
                validate(self.info, engine, compose)

    def test_classic_store_and_wrong_architecture_are_refused(self):
        for changes in [{'DriverStatus': []}, {'Architecture': 'aarch64'}, {'OSType': 'windows'}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate({**self.info, **changes}, '29.1.3', '2.40.3+ds1-0ubuntu1~24.04.1')
