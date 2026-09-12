"""Execute the workflow's actual Python blocks with synthetic metadata and a fake AWS CLI."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest


WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/production-oidc-preflight.yml"
ACCOUNT = "123456789012"
INSTANCE = "zavliq-production"
INSTANCE_ARN = f"arn:aws:lightsail:us-east-1:{ACCOUNT}:Instance/synthetic-instance"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/zavliq-production-github"
SESSION_ARN = f"arn:aws:sts::{ACCOUNT}:assumed-role/zavliq-production-github/zavliq-oidc-123-1"
SENTINEL = "PRIVATE_RESPONSE_SENTINEL"


def embedded_scripts(source: str) -> list[str]:
    """Extract literal run blocks, preserving their real Python rather than duplicating it."""
    lines = source.splitlines()
    scripts = []
    for index, line in enumerate(lines):
        if line.strip() != "run: |":
            continue
        indent = len(line) - len(line.lstrip())
        block = []
        for following in lines[index + 1:]:
            if following.strip() and len(following) - len(following.lstrip()) <= indent:
                break
            block.append(following)
        body = textwrap.dedent("\n".join(block)).strip().splitlines()
        if body[0] != "python3 -I -B - <<'PY'" or body[-1] != "PY":
            raise AssertionError("Unexpected workflow script wrapper")
        script = "\n".join(body[1:-1]) + "\n"
        compile(script, str(WORKFLOW), "exec")
        scripts.append(script)
    if len(scripts) != 2:
        raise AssertionError("Expected only input validation and read-only identity verification")
    return scripts


class OidcPreflightTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = WORKFLOW.read_text()
        cls.scripts = embedded_scripts(cls.source)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.log = self.directory / "calls.jsonl"
        self.fixture = self.directory / "responses.json"
        # PATH contains only this executable. These tests cannot fall back to a real AWS CLI.
        fake = self.directory / "aws"
        fake.write_text(f"#!{sys.executable}\n" + textwrap.dedent("""\
            import json, os, sys, time
            from pathlib import Path
            with open(os.environ['FAKE_LOG'], 'a') as stream:
                stream.write(json.dumps({'argv': sys.argv[1:], 'pid': os.getpid(),
                    'max_attempts': os.environ.get('AWS_MAX_ATTEMPTS'),
                    'pager': os.environ.get('AWS_PAGER'),
                    'metadata_disabled': os.environ.get('AWS_EC2_METADATA_DISABLED')}) + '\\n')
            fixture = json.loads(Path(os.environ['FAKE_FIXTURE']).read_text())
            response = fixture['identity' if sys.argv[1] == 'sts' else 'instance']
            time.sleep(response.get('sleep', 0))
            sys.stdout.write(response.get('raw', json.dumps(response.get('body', {}))))
            sys.stderr.write(response.get('stderr', ''))
            raise SystemExit(response.get('exit', 0))
            """))
        fake.chmod(0o700)
        self.env = {
            "PATH": str(self.directory),
            "EXPECTED_ACCOUNT": ACCOUNT, "INSTANCE": INSTANCE,
            "INSTANCE_ARN": INSTANCE_ARN, "DEPLOY_ROLE": ROLE,
            "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1",
            "ACTIONS_ID_TOKEN_REQUEST_URL": "https://synthetic.invalid/token",
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN": SENTINEL,
            "FAKE_LOG": str(self.log), "FAKE_FIXTURE": str(self.fixture),
        }
        self.responses = {
            "identity": {"body": {"Account": ACCOUNT, "Arn": SESSION_ARN}},
            "instance": {"body": {"instance": {
                "name": INSTANCE, "arn": INSTANCE_ARN,
                "location": {"regionName": "us-east-1"}, "state": {"name": "running"},
                "unneeded_private_field": SENTINEL,
            }}},
        }

    def run_script(self, index, env=None):
        self.fixture.write_text(json.dumps(self.responses))
        return subprocess.run([sys.executable, "-I", "-B", "-c", self.scripts[index]],
                              env=self.env if env is None else env,
                              capture_output=True, text=True, timeout=25)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def assert_sanitized(self, result):
        self.assertEqual(result.stderr, "")
        for private in (ACCOUNT, INSTANCE_ARN, ROLE, SESSION_ARN, SENTINEL, INSTANCE):
            self.assertNotIn(private, result.stdout)

    def assert_failure(self, result, code):
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout), {"ok": False, "code": code})
        self.assert_sanitized(result)

    def test_manual_protected_envelope_and_regional_read_policy(self):
        self.assertRegex(self.source, r"(?m)^on:\n  workflow_dispatch:\npermissions:")
        self.assertIn("if: github.repository == 'IsmailKharoub/zavliq' && github.ref == 'refs/heads/main'", self.source)
        self.assertIn("    environment: production\n    timeout-minutes: 5", self.source)
        self.assertIn("group: production-host-access\n  cancel-in-progress: false", self.source)
        self.assertEqual(re.findall(r"uses: (\S+)", self.source), [
            "aws-actions/configure-aws-credentials@cbe3b392738ccf3f987d68400dafcf4b0624a56c"])
        self.assertNotIn("actions/checkout", self.source)
        self.assertNotIn("${{ vars.", self.source)
        self.assertEqual(set(re.findall(r"secrets\.([A-Z_]+)", self.source)), {
            "AWS_ACCOUNT_ID", "AWS_DEPLOY_ROLE_ARN", "LIGHTSAIL_INSTANCE", "LIGHTSAIL_INSTANCE_ARN"})
        self.assertIn("force-skip-oidc: false", self.source)
        self.assertIn("use-existing-credentials: false", self.source)
        self.assertIn("unset-current-credentials: true", self.source)
        self.assertIn("output-credentials: false", self.source)
        self.assertIn("mask-aws-account-id: true", self.source)
        policy_text = self.source.split("inline-session-policy: >-\n", 1)[1].split("      - name:", 1)[0]
        policy = json.loads(policy_text.replace("${{ secrets.LIGHTSAIL_INSTANCE_ARN }}", INSTANCE_ARN))
        self.assertEqual(policy, {"Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": "sts:GetCallerIdentity", "Resource": "*"},
            {"Effect": "Allow", "Action": "lightsail:GetInstance", "Resource": "*",
             "Condition": {"StringEquals": {"aws:RequestedRegion": "us-east-1"}}},
        ]})

    def test_valid_original_and_distinct_replacement_inputs(self):
        for name in (INSTANCE, "zavliq-production-recovery-reviewed-1"):
            with self.subTest(name=name):
                result = self.run_script(0, {**self.env, "INSTANCE": name})
                self.assertEqual(result.returncode, 0)
                self.assertEqual(json.loads(result.stdout), {"protected_inputs_valid": True})
                self.assert_sanitized(result)
        self.assertEqual(self.calls(), [])

    def test_missing_metadata_or_oidc_context_fails_before_aws(self):
        for key in ("EXPECTED_ACCOUNT", "INSTANCE", "INSTANCE_ARN", "DEPLOY_ROLE",
                    "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT",
                    "ACTIONS_ID_TOKEN_REQUEST_URL", "ACTIONS_ID_TOKEN_REQUEST_TOKEN"):
            with self.subTest(key=key):
                self.assert_failure(self.run_script(0, {**self.env, key: ""}), "PREFLIGHT_INPUTS_INVALID")
        self.assertEqual(self.calls(), [])

    def test_inconsistent_metadata_is_rejected_before_aws(self):
        invalid = [
            ("EXPECTED_ACCOUNT", "123"), ("INSTANCE", "zavliq-staging"),
            ("INSTANCE", "zavliq-production-recovery-"),
            ("INSTANCE", "zavliq-production-recovery-" + "a" * 64),
            ("INSTANCE_ARN", INSTANCE_ARN.replace("us-east-1", "us-west-2")),
            ("INSTANCE_ARN", INSTANCE_ARN.replace(ACCOUNT, "999999999999")),
            ("INSTANCE_ARN", INSTANCE_ARN + "*"),
            ("DEPLOY_ROLE", ROLE.replace("github", "unreviewed")),
            ("DEPLOY_ROLE", ROLE.replace(ACCOUNT, "999999999999")),
            ("GITHUB_RUN_ID", "1\n2"), ("GITHUB_RUN_ATTEMPT", "1" * 21),
        ]
        for key, value in invalid:
            with self.subTest(key=key):
                self.assert_failure(self.run_script(0, {**self.env, key: value}), "PREFLIGHT_INPUTS_INVALID")
        self.assertEqual(self.calls(), [])

    def test_success_reads_only_exact_identity_and_instance(self):
        result = self.run_script(1)
        self.assertEqual(result.returncode, 0)
        output = json.loads(result.stdout)
        self.assertTrue(output["ok"])
        self.assertFalse(output["deployment_or_backup_verified"])
        self.assert_sanitized(result)
        calls = self.calls()
        self.assertEqual(len(calls), 2)
        common = ["--region", "us-east-1", "--output", "json", "--cli-connect-timeout", "5",
                  "--cli-read-timeout", "10", "--no-cli-pager"]
        self.assertEqual(calls[0]["argv"], ["sts", "get-caller-identity", *common])
        self.assertEqual(calls[1]["argv"], ["lightsail", "get-instance", "--instance-name", INSTANCE, *common])
        for call in calls:
            self.assertEqual((call["max_attempts"], call["pager"], call["metadata_disabled"]), ("1", "", "true"))

    def test_wrong_account_role_or_session_never_reads_instance(self):
        for changed, code in (
                ({"Account": "999999999999", "Arn": SESSION_ARN}, "PREFLIGHT_ACCOUNT_MISMATCH"),
                ({"Account": ACCOUNT, "Arn": SESSION_ARN.replace("github/", "unreviewed/")}, "PREFLIGHT_ROLE_SESSION_MISMATCH"),
                ({"Account": ACCOUNT, "Arn": SESSION_ARN.replace("-123-1", "-123-2")}, "PREFLIGHT_ROLE_SESSION_MISMATCH")):
            with self.subTest(identity_mismatch=True):
                self.log.unlink(missing_ok=True)
                self.responses["identity"]["body"] = changed
                self.assert_failure(self.run_script(1), code)
                self.assertEqual(len(self.calls()), 1)

    def test_instance_name_arn_region_and_running_state_are_required(self):
        original = self.responses["instance"]["body"]["instance"]
        for patch, code in (({"name": "zavliq-production-recovery-other"}, "PREFLIGHT_INSTANCE_IDENTITY_MISMATCH"),
                            ({"arn": INSTANCE_ARN + "replacement"}, "PREFLIGHT_INSTANCE_IDENTITY_MISMATCH"),
                            ({"location": {"regionName": "us-west-2"}}, "PREFLIGHT_INSTANCE_REGION_MISMATCH"),
                            ({"state": {"name": "stopped"}}, "PREFLIGHT_INSTANCE_NOT_RUNNING"),
                            ({"location": None}, "PREFLIGHT_INSTANCE_RESPONSE_INVALID")):
            with self.subTest(instance_mismatch=True):
                self.responses["instance"]["body"]["instance"] = {**original, **patch}
                self.assert_failure(self.run_script(1), code)

    def test_failed_malformed_and_oversized_responses_are_redacted(self):
        for response, suffix in (({"exit": 1, "raw": SENTINEL, "stderr": SENTINEL}, "FAILED"),
                                 ({"raw": SENTINEL}, "RESPONSE_INVALID"),
                                 ({"raw": "[1,2,3]"}, "RESPONSE_INVALID"),
                                 ({"raw": " " * 65537 + SENTINEL}, "FAILED")):
            with self.subTest(response_failure=True):
                self.responses["identity"] = response
                self.assert_failure(self.run_script(1), "PREFLIGHT_CALLER_IDENTITY_REQUEST_" + suffix)

    def test_instance_read_failure_is_distinguished_without_response_text(self):
        self.responses["instance"] = {"exit": 254, "raw": SENTINEL,
                                      "stderr": "AccessDeniedException: " + INSTANCE_ARN + SENTINEL}
        self.assert_failure(self.run_script(1), "PREFLIGHT_INSTANCE_READ_FAILED")
        self.assertEqual(len(self.calls()), 2)

    def test_missing_instance_object_is_an_explicit_schema_failure(self):
        self.responses["instance"] = {"body": {"unexpected": SENTINEL}}
        self.assert_failure(self.run_script(1), "PREFLIGHT_INSTANCE_RESPONSE_INVALID")

    def test_unavailable_cli_never_falls_back_to_a_real_executable(self):
        (self.directory / "aws").unlink()
        self.assert_failure(self.run_script(1), "PREFLIGHT_CALLER_IDENTITY_REQUEST_CLI_UNAVAILABLE")
        self.assertEqual(self.calls(), [])

    def test_actual_subprocess_timeout_is_bounded_and_reaps_cli(self):
        self.responses["identity"] = {"sleep": 30, "raw": SENTINEL}
        started = time.monotonic()
        self.assert_failure(self.run_script(1), "PREFLIGHT_CALLER_IDENTITY_REQUEST_TIMEOUT")
        self.assertLess(time.monotonic() - started, 20)
        calls = self.calls()
        self.assertEqual(len(calls), 1)
        with self.assertRaises(ProcessLookupError):
            os.kill(calls[0]["pid"], 0)


if __name__ == "__main__":
    unittest.main()
