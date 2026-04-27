#!/usr/bin/env python3
"""Integration tests for the memsleuth CLI.

Run with::

    python3 -m unittest tests.test_cli
    # or
    ./tests/test_cli.py

Each test invokes memsleuth.py as a subprocess, asserts on the exit
code, and looks for stable substrings in the output. Tests use only
non-destructive flags (or --dry-run) so they're safe to run repeatedly
on any host. Tests that exercise root-only behaviour are gated on
``os.geteuid()``.
"""

import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "memsleuth.py"


def run(*args, expect_rc=0, timeout=60):
    """Invoke memsleuth.py with args; return (stdout, stderr, rc)."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT)] + list(args),
        capture_output=True, text=True, timeout=timeout,
    )
    if expect_rc is not None and result.returncode != expect_rc:
        raise AssertionError(
            "expected rc={} got {}\nstdout:\n{}\nstderr:\n{}".format(
                expect_rc, result.returncode, result.stdout, result.stderr))
    return result.stdout, result.stderr, result.returncode


class TestHelp(unittest.TestCase):
    def test_help(self):
        out, _, _ = run("--help")
        for flag in ("--procs", "--shared", "--containers", "--numa",
                     "--release", "--unlink", "--dry-run", "--doctor",
                     "--low-mem-pct", "--low-mem-max", "--help-fields"):
            self.assertIn(flag, out)

    def test_help_fields(self):
        out, _, _ = run("--help-fields")
        for header in ("Top summary", "Hugepage allocation capacity",
                       "Hugetlbfs file summary", "Health check (--doctor)"):
            self.assertIn(header, out)


class TestDefaultReport(unittest.TestCase):
    def test_default(self):
        out, _, _ = run()
        self.assertIn("Memory", out)
        self.assertIn("HugeTLB Pages", out)

    def test_no_thp_no_directmap(self):
        out, _, _ = run("--no-thp", "--no-directmap")
        self.assertNotIn("Transparent Huge Pages", out)
        self.assertNotIn("Kernel Direct Map", out)


class TestProcessFlags(unittest.TestCase):
    def test_procs(self):
        out, _, _ = run("--procs", "--top", "3")
        self.assertIn("Per-process memory detail", out)

    def test_shared_implies_procs(self):
        out, _, _ = run("--shared", "--top", "2")
        self.assertIn("Per-process memory detail", out)

    def test_containers_implies_procs(self):
        out, _, _ = run("--containers", "--top", "2")
        self.assertIn("Per-process memory detail", out)

    def test_procs_top_zero_runs(self):
        # --top 0 means show all; just verify it doesn't crash.
        run("--procs", "--top", "0", timeout=120)


class TestNuma(unittest.TestCase):
    def test_numa(self):
        out, _, _ = run("--numa")
        # NUMA-specific section may or may not show depending on host;
        # what we care about is that the run succeeded and the standard
        # hugetlb section is still present.
        self.assertIn("HugeTLB Pages", out)

    def test_procs_numa(self):
        out, _, _ = run("--procs", "--numa", "--top", "3")
        self.assertIn("Per-process memory detail", out)


class TestCombinations(unittest.TestCase):
    def test_procs_shared_containers_numa(self):
        out, _, _ = run("--procs", "--shared", "--containers",
                         "--numa", "--top", "2")
        self.assertIn("Per-process memory detail", out)

    def test_procs_with_no_thp(self):
        out, _, _ = run("--procs", "--no-thp", "--top", "2")
        self.assertIn("Per-process memory detail", out)
        self.assertNotIn("Transparent Huge Pages", out)


class TestDoctor(unittest.TestCase):
    def test_doctor_runs(self):
        out, _, _ = run("--doctor")
        self.assertIn("memsleuth doctor", out)

    def test_doctor_force_low_mem(self):
        # 100% of MemTotal capped at a huge ceiling -> threshold == MemTotal,
        # which is always above MemAvailable so the alert always fires.
        out, _, _ = run("--doctor", "--low-mem-pct", "100",
                         "--low-mem-max", "1024T")
        self.assertIn("Low available memory", out)
        self.assertIn("Top 5 RSS users", out)

    def test_doctor_zero_threshold_silences_low_mem(self):
        # max=0 -> threshold=0 -> low-mem check disabled.
        out, _, _ = run("--doctor", "--low-mem-pct", "100", "--low-mem-max", "0")
        self.assertNotIn("Low available memory", out)

    def test_doctor_invalid_size(self):
        _, err, _ = run("--low-mem-max", "bogus", "--doctor", expect_rc=2)
        self.assertIn("invalid size", err)


class TestDestructiveDryRun(unittest.TestCase):
    """--dry-run for --release / --unlink works without root and never modifies state."""

    def test_release_dry_run(self):
        out, _, _ = run("--release", "--dry-run")
        self.assertIn("Release hugepages", out)

    def test_unlink_dry_run(self):
        out, _, _ = run("--unlink", "--dry-run")
        self.assertIn("Unlink unused hugetlbfs files", out)

    def test_unlink_then_release_order(self):
        out, _, _ = run("--unlink", "--release", "--dry-run")
        self.assertIn("Unlink unused hugetlbfs files", out)
        self.assertIn("Release hugepages", out)
        # --unlink runs before --release so its banner appears first.
        self.assertLess(out.index("Unlink unused hugetlbfs"),
                         out.index("Release hugepages"))


@unittest.skipIf(os.geteuid() == 0,
                  "destructive flags only behave non-trivially as non-root")
class TestDestructiveRequiresRoot(unittest.TestCase):
    def test_release_requires_root(self):
        _, err, _ = run("--release", expect_rc=1)
        self.assertIn("require root", err)

    def test_unlink_requires_root(self):
        _, err, _ = run("--unlink", expect_rc=1)
        self.assertIn("require root", err)

    def test_release_unlink_requires_root(self):
        _, err, _ = run("--release", "--unlink", expect_rc=1)
        self.assertIn("require root", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
