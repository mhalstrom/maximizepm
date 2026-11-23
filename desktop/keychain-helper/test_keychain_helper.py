"""Opt-in macOS integration test using dummy items in the default keychain."""

import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path


SOURCE = Path(__file__).with_name("keychain-helper.c")
SECURITY = "/usr/bin/security"
MAX_VALUE_BYTES = 4 * ((64 * 1024 + 2) // 3) + 4


class KeychainHelperTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        build = tempfile.TemporaryDirectory()
        cls.addClassCleanup(build.cleanup)
        folder = Path(build.name)
        cls.helper = folder / "keychain-helper"
        cls.other = folder / "other-reader"
        for binary in (cls.helper, cls.other):
            subprocess.run(
                ["xcrun", "clang", "-std=c11", "-mmacosx-version-min=12.0",
                 "-Wall", "-Wextra", "-Werror", "-Wno-deprecated-declarations",
                 "-framework", "Security", "-framework", "CoreFoundation",
                 SOURCE, "-o", binary],
                check=True, timeout=30,
            )
            subprocess.run(
                ["codesign", "--force", "--sign", "-", "--identifier",
                 f"com.maximizepm.test.{binary.name}", binary],
                check=True, timeout=10,
            )

    def setUp(self):
        self.service = f"MaximizePM test {uuid.uuid4().hex}"
        self.account = "dummy"
        self.addCleanup(self.call, self.helper, "delete", expected=0)

    def call(self, program, command, data=None, *, expected=0):
        result = subprocess.run(
            [program, command, self.service, self.account], input=data,
            capture_output=True, timeout=5,
        )
        self.assertEqual(result.returncode, expected, result.stderr.decode())
        if command != "get" or expected != 0:
            self.assertEqual(result.stdout, b"")
        return result.stdout

    def test_replace_preserves_bytes_and_keeps_other_reader_out(self):
        self.call(self.helper, "get", expected=2)
        for value in (b"dummy\0value\n", b"x" * MAX_VALUE_BYTES):
            self.call(self.helper, "put", value)
            self.assertEqual(self.call(self.helper, "get"), value)
            self.call(self.other, "get", expected=1)
        for invalid in (b"", b"x" * (MAX_VALUE_BYTES + 1)):
            self.call(self.helper, "put", invalid, expected=1)
        self.assertEqual(self.call(self.helper, "get"), value)
        self.call(self.helper, "delete")
        self.call(self.helper, "get", expected=2)

    def test_put_refuses_to_write_over_a_foreign_acl(self):
        self.call(self.other, "put", b"other dummy")
        self.call(self.helper, "put", b"replacement dummy", expected=1)
        self.assertEqual(self.call(self.other, "get"), b"other dummy")

    def test_put_refuses_to_reuse_an_empty_foreign_item(self):
        subprocess.run(
            [SECURITY, "add-generic-password", "-s", self.service,
             "-a", self.account, "-w", ""],
            check=True, capture_output=True, timeout=5,
        )
        self.call(self.helper, "put", b"replacement dummy", expected=1)


if __name__ == "__main__":
    unittest.main()
