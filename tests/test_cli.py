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

import importlib.util
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "memsleuth.py"


def _load_memsleuth():
    """Import memsleuth.py as a module for unit-testing pure helpers."""
    spec = importlib.util.spec_from_file_location("memsleuth", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


memsleuth = _load_memsleuth()


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

    def test_defrag_dry_run(self):
        out, _, _ = run("--defrag", "--dry-run")
        self.assertIn("Defrag memory", out)
        self.assertIn("WOULD", out)

    def test_grow_dry_run(self):
        out, _, _ = run("--grow", "2M:0:8", "--dry-run")
        self.assertIn("Grow hugepages", out)

    def test_grow_bad_spec_is_reported_not_crash(self):
        out, _, rc = run("--grow", "nonsense", "--dry-run")
        self.assertIn("Grow hugepages", out)
        self.assertEqual(rc, 0)

    def test_destructive_order(self):
        out, _, _ = run("--unlink", "--release", "--defrag", "--grow", "2M:0:1", "--dry-run")
        # unlink -> release -> defrag -> grow
        order = [out.index(s) for s in ("Unlink unused hugetlbfs", "Release hugepages",
                                        "Defrag memory", "Grow hugepages")]
        self.assertEqual(order, sorted(order))


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

    def test_defrag_requires_root(self):
        _, err, _ = run("--defrag", expect_rc=1)
        self.assertIn("require root", err)

    def test_grow_requires_root(self):
        _, err, _ = run("--grow", "2M:0:1", expect_rc=1)
        self.assertIn("require root", err)


class TestVersion(unittest.TestCase):
    def test_version_long(self):
        out, _, _ = run("--version")
        self.assertIn("memsleuth", out)

    def test_version_short(self):
        out, _, _ = run("-V")
        self.assertIn("memsleuth", out)


class TestParseGrowSpec(unittest.TestCase):
    SIZES = {2 * 1024 * 1024: "hugepages-2048kB", 1024 * 1024 * 1024: "hugepages-1048576kB"}
    NODES = [0, 1]

    def p(self, spec):
        return memsleuth._parse_grow_spec(spec, self.SIZES, self.NODES)

    def test_valid_2m(self):
        self.assertEqual(self.p("2M:0:512"), (2 * 1024 * 1024, 0, 512))

    def test_valid_1g_lowercase(self):
        self.assertEqual(self.p("1g:1:8"), (1024 * 1024 * 1024, 1, 8))

    def test_bad_arity(self):
        with self.assertRaises(ValueError):
            self.p("2M:0")

    def test_unknown_size(self):
        with self.assertRaises(ValueError):
            self.p("4M:0:1")

    def test_size_not_configured(self):
        with self.assertRaises(ValueError):
            memsleuth._parse_grow_spec("1G:0:1", {2 * 1024 * 1024: "hugepages-2048kB"}, [0])

    def test_node_not_online(self):
        with self.assertRaises(ValueError):
            self.p("2M:5:1")

    def test_bad_count(self):
        with self.assertRaises(ValueError):
            self.p("2M:0:x")

    def test_negative_count(self):
        with self.assertRaises(ValueError):
            self.p("2M:0:-3")


class TestReleaseTarget(unittest.TestCase):
    """Pure arithmetic behind --release. `nr` reads back as the total pool
    (already includes surplus); the target keeps reserved + in-use pages."""

    def test_no_reservations(self):
        # nr=100 total, 30 free, none reserved -> keep 70 in use.
        self.assertEqual(memsleuth._release_target(100, 30, 0), 70)

    def test_reserved_pages_are_kept(self):
        # 30 free of which 5 reserved -> release 25, keep 75 (70 in use + 5 rsvd).
        self.assertEqual(memsleuth._release_target(100, 30, 5), 75)

    def test_surplus_included_in_nr(self):
        # nr already includes surplus, so once surplus is absorbed the total is
        # still nr. nr=150 total, 30 free, 0 reserved -> keep 120.
        self.assertEqual(memsleuth._release_target(150, 30, 0), 120)

    def test_all_free_reserved_releases_nothing(self):
        self.assertEqual(memsleuth._release_target(100, 5, 5), 100)

    def test_nothing_free(self):
        self.assertEqual(memsleuth._release_target(100, 0, 0), 100)


class TestHugepageDoctorFinding(unittest.TestCase):
    """Doctor finding logic per pool. `nr` includes surplus; the check fires on
    releasable free pages (free - resv) OR surplus > 0, both fixed by --release."""

    @staticmethod
    def pool(nr, free, resv=0, surplus=0, size=2 * 1024 * 1024):
        return {"size": size, "nr": nr, "free": free, "resv": resv,
                "surplus": surplus, "overcommit": 0}

    def test_healthy_pool_no_finding(self):
        # all pages in use, none free, no surplus -> nothing to report.
        self.assertIsNone(memsleuth._hugepage_doctor_finding(self.pool(10, 0)))

    def test_empty_pool_no_finding(self):
        self.assertIsNone(memsleuth._hugepage_doctor_finding(self.pool(0, 0)))

    def test_all_free_reserved_no_finding(self):
        # free pages exist but all reserved, no surplus -> nothing releasable.
        self.assertIsNone(memsleuth._hugepage_doctor_finding(self.pool(10, 5, resv=5)))

    def test_free_pages_only(self):
        f = memsleuth._hugepage_doctor_finding(self.pool(10, 4))
        self.assertIsNotNone(f)
        self.assertIn("4 free pages", f["title"])
        self.assertNotIn("surplus", f["title"])

    def test_surplus_only_fires(self):
        # the 'echo 0 > nr_hugepages while in use' aftermath: all surplus, in use.
        f = memsleuth._hugepage_doctor_finding(self.pool(10, 0, surplus=10))
        self.assertIsNotNone(f)
        self.assertIn("10 surplus pages", f["title"])
        self.assertIn("absorb", f["recommendation"])

    def test_composite_surplus_and_free(self):
        # NUMA composite: free on one node, surplus in use on another.
        f = memsleuth._hugepage_doctor_finding(self.pool(28, 8, surplus=20))
        self.assertIsNotNone(f)
        self.assertIn("20 surplus pages", f["title"])
        self.assertIn("8 releasable free pages", f["title"])
        self.assertIn("20 in use", f["title"])  # nr - free, not double-counted

    def test_reserved_note_when_freeing(self):
        f = memsleuth._hugepage_doctor_finding(self.pool(10, 8, resv=3))
        self.assertIn("reserved page(s) are kept", f["recommendation"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
