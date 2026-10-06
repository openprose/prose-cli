"""Provider-free signing failure and evidence binding tests."""
import hashlib
import json
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import sign_macos as signing

TEAM = "ABCDE12345"
IDENTITY = f"Developer ID Application: OpenProse ({TEAM})"
ISSUER = "00000000-0000-4000-8000-000000000001"
SUBMISSION = "00000000-0000-4000-8000-000000000002"


class SigningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ("bun", "rust", "key"):
            (self.root / name).write_bytes(name.encode())
        self.output = self.root / "signed"
        self.calls = []
        self.log_status = "Accepted"
        self.log_digest = None
        self.detail_team = TEAM
        self.entitlements = signing.BUN_ENTITLEMENTS
        self.platform = patch.object(signing.platform, "system", return_value="Darwin")
        self.platform.start()
        self.addCleanup(self.platform.stop)

    def native(self, args):
        self.calls.append(args)
        if "--sign" in args:
            target = Path(args[-1])
            target.write_bytes(target.read_bytes() + b"-signed")
        if "--verbose=4" in args:
            return f"Authority={IDENTITY}\nTeamIdentifier={self.detail_team}\nTimestamp=2026-09-17\nCodeDirectory v=20500 flags=0x10000(runtime)\n"
        if "--entitlements" in args and "--display" in args:
            return plistlib.dumps(self.entitlements).decode() if args[-1].endswith("prose-bun") else "Executable=prose-rust\n"
        if '--packaged-self-test' in args:
            import kernel_rc_evidence as custody
            return json.dumps(custody.SDK_IMPORT_TEST)
        if '--packaged-tool-self-test' in args:
            import kernel_rc_evidence as custody
            return json.dumps(custody.SDK_TOOL_TEST)
        if "submit" in args:
            return json.dumps({"id": SUBMISSION, "status": "Accepted"})
        if "log" in args:
            return json.dumps({"jobId": SUBMISSION, "status": self.log_status,
                               "statusCode": 0, "sha256": self.log_digest or signing.sha256(self.output / "notarization.zip")})
        return ""

    def kwargs(self):
        return dict(identity=IDENTITY, team_id=TEAM, notary_key=self.root / "key",
                    notary_key_id="1234567890", notary_issuer=ISSUER, commands=self.native)

    def sign(self, **overrides):
        args = dict(bun=self.root / "bun", rust=self.root / "rust", output=self.output, **self.kwargs())
        args.update(overrides)
        return signing.sign(**args)

    def test_success_preserves_inputs_and_binds_exact_signed_archive(self):
        receipt = self.sign()
        self.assertEqual((self.root / "bun").read_bytes(), b"bun")
        self.assertEqual((self.root / "rust").read_bytes(), b"rust")
        self.assertEqual(receipt["binaries"]["bun"]["signedSha256"], hashlib.sha256(b"bun-signed").hexdigest())
        self.assertEqual(signing.verify_existing(self.output, **self.kwargs()), receipt)
        self.assertEqual(sum("submit" in call for call in self.calls), 1)
        self.assertEqual(sum("log" in call for call in self.calls), 2)
        for call in self.calls:
            if "--sign" in call:
                self.assertIn("--timestamp", call)
                self.assertIn("runtime", call)
                self.assertEqual("--entitlements" in call, call[-1].endswith("prose-bun"))

    def test_sdk_missing_inner_signing_receipt_fails_before_native_commands(self):
        helper = self.root / 'sdk'; helper.write_bytes(b'sdk')
        with self.assertRaises(signing.SigningError):
            self.sign(agents_sdk=helper)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.output.exists())

    def test_sdk_helper_signed_and_notarized_with_both_runners(self):
        helper = self.root / 'sdk'; helper.write_bytes(b'sdk')
        notices = self.root / 'AGENTS-SDK-NOTICES.txt'; notices.write_bytes(b'notices')
        build = {'helper': {'path': 'prose-agents-sdk', 'byteLength': 3, 'sha256': signing.sha256(helper)},
                 'notices': {'path': notices.name, 'byteLength': 7, 'sha256': signing.sha256(notices)},
                 'embeddedSigning': {'identity': IDENTITY, 'verification': 'pyinstaller-inner-binaries-and-frozen-self-tests'}}
        (self.root / 'agents-sdk-build.json').write_text(json.dumps(build))
        receipt = self.sign(agents_sdk=helper)
        bound = json.loads((self.output / 'agents-sdk-build.json').read_text())
        self.assertEqual(bound['helper']['sha256'], receipt['binaries']['agents-sdk']['signedSha256'])
        self.assertEqual(bound['signingReceiptSha256'], signing.sha256(self.output / 'receipt.json'))
        self.assertEqual(set(receipt['binaries']), {'bun', 'rust', 'agents-sdk'})
        self.assertEqual(receipt['binaries']['agents-sdk']['signedSha256'], hashlib.sha256(b'sdk-signed').hexdigest())
        self.assertEqual(signing.verify_existing(self.output, **self.kwargs()), receipt)
        with zipfile.ZipFile(self.output / 'notarization.zip') as archive:
            self.assertEqual(set(archive.namelist()), {'prose-bun', 'prose-rust', 'prose-agents-sdk'})

    def test_non_macos_makes_no_native_calls(self):
        with patch.object(signing.platform, "system", return_value="Linux"):
            with self.assertRaises(signing.SigningError):
                self.sign()
        self.assertEqual(self.calls, [])

    def test_ad_hoc_and_wrong_identity_rejected_before_sign(self):
        for identity in ("-", "Apple Development: OpenProse", "Developer ID Application: Other (ZZZZZ12345)"):
            with self.assertRaises(signing.SigningError):
                self.sign(identity=identity)
        self.assertEqual(self.calls, [])

    def test_existing_output_and_symlink_inputs_rejected(self):
        self.output.mkdir()
        with self.assertRaises(signing.SigningError):
            self.sign()
        self.output.rmdir()
        link = self.root / "link"
        link.symlink_to(self.root / "bun")
        with self.assertRaises(signing.SigningError):
            self.sign(bun=link)
        self.assertEqual(self.calls, [])

    def test_wrong_signature_team_fails_before_submission(self):
        self.detail_team = "XXXXX12345"
        with self.assertRaises(signing.SigningError):
            self.sign()
        self.assertFalse(any("submit" in call for call in self.calls))
        self.assertFalse((self.output / "receipt.json").exists())

    def test_unexpected_entitlements_fail_before_submission(self):
        self.entitlements = {"com.apple.security.get-task-allow": True}
        with self.assertRaises(signing.SigningError):
            self.sign()
        self.assertFalse(any("submit" in call for call in self.calls))

    def test_rejected_and_wrong_archive_logs_issue_no_receipt(self):
        self.log_status = "Invalid"
        with self.assertRaises(signing.SigningError):
            self.sign()
        self.assertFalse((self.output / "receipt.json").exists())
        self.output = self.root / "signed-second"
        self.log_status = "Accepted"
        self.log_digest = "0" * 64
        with self.assertRaises(signing.SigningError):
            self.sign()
        self.assertFalse((self.output / "receipt.json").exists())

    def test_verify_rejects_changed_binary_without_resigning(self):
        self.sign()
        self.calls.clear()
        (self.output / "prose-bun").write_bytes(b"tampered")
        with self.assertRaises(signing.SigningError):
            signing.verify_existing(self.output, **self.kwargs())
        self.assertEqual(self.calls, [])

    def test_verify_archive_contents_not_just_receipt_digest(self):
        self.sign()
        archive = self.output / "notarization.zip"
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("prose-bun", b"wrong-binary")
            zipped.writestr("prose-rust", b"rust-signed")
        receipt_path = self.output / "receipt.json"
        receipt = json.loads(receipt_path.read_text())
        receipt["notarization"]["sha256"] = signing.sha256(archive)
        receipt_path.write_text(json.dumps(receipt))
        self.calls.clear()
        with self.assertRaises(signing.SigningError):
            signing.verify_existing(self.output, **self.kwargs())
        self.assertFalse(any("log" in call for call in self.calls))

    def test_verify_asks_apple_again_and_rejects_log_mismatch(self):
        self.sign()
        self.log_digest = "0" * 64
        with self.assertRaises(signing.SigningError):
            signing.verify_existing(self.output, **self.kwargs())

    def test_command_failure_does_not_expose_native_secrets(self):
        result = subprocess.CompletedProcess([], 1, "secret-output", "secret-error")
        with patch.object(signing.subprocess, "run", return_value=result) as runner:
            with self.assertRaises(signing.SigningError) as raised:
                signing.Commands(30)(["/usr/bin/xcrun", "notarytool"])
        self.assertNotIn("secret", str(raised.exception))
        self.assertEqual(runner.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertNotIn("OPENAI_API_KEY", runner.call_args.kwargs["env"])

    def test_deadline_timeout_is_redacted_and_no_retry(self):
        with patch.object(signing.subprocess, "run", side_effect=subprocess.TimeoutExpired(["secret"], 1)) as runner:
            with self.assertRaises(signing.SigningError) as raised:
                signing.Commands(30)(["/usr/bin/xcrun", "notarytool"])
        self.assertNotIn("secret", str(raised.exception))
        self.assertEqual(runner.call_count, 1)


if __name__ == "__main__":
    unittest.main()
