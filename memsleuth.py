#!/usr/bin/env python3
"""memsleuth - show Linux memory usage with a full hugepages breakdown.

Reads /proc/meminfo for overall memory (like `free`) and walks
/sys/kernel/mm/hugepages/ to report every configured hugepage size with
total/free/reserved/surplus counts. Optionally breaks the hugepage pools
down per NUMA node.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

HUGEPAGES_ROOT = Path("/sys/kernel/mm/hugepages")
NUMA_ROOT = Path("/sys/devices/system/node")
MEMINFO = Path("/proc/meminfo")
PROC = Path("/proc")

PROC_HP_FIELDS = ("AnonHugePages", "ShmemPmdMapped", "FilePmdMapped",
                  "Shared_Hugetlb", "Private_Hugetlb")

SMAPS_HEADER_RE = re.compile(
    r"^[0-9a-f]+-[0-9a-f]+\s+(?P<perms>\S+)\s+\S+\s+\S+\s+\S+(?:\s+(?P<path>.*))?$"
)
SMAPS_FIELDS = {
    "Size": "size",
    "Rss": "rss",
    "Pss": "pss",
    "Shared_Clean": "shared_clean",
    "Shared_Dirty": "shared_dirty",
    "Swap": "swap",
    "AnonHugePages": "anon_thp",
    "ShmemPmdMapped": "shmem_pmd",
    "FilePmdMapped": "file_pmd",
    "Private_Hugetlb": "hugetlb_priv",
    "Shared_Hugetlb": "hugetlb_shared",
}
SMAPS_INT_ATTRS = tuple(SMAPS_FIELDS.values())

UNITS = [("PiB", 1 << 50), ("TiB", 1 << 40), ("GiB", 1 << 30),
         ("MiB", 1 << 20), ("KiB", 1 << 10)]


def human(nbytes: int, *, zero: str = "0 B") -> str:
    if nbytes == 0:
        return zero
    neg = nbytes < 0
    n = -nbytes if neg else nbytes
    for unit, scale in UNITS:
        if n >= scale:
            return f"{'-' if neg else ''}{n / scale:,.2f} {unit}"
    return f"{'-' if neg else ''}{n} B"


def parse_meminfo(path: Path = MEMINFO) -> dict[str, int]:
    """Return /proc/meminfo as a dict of name -> bytes."""
    fields: dict[str, int] = {}
    for line in path.read_text().splitlines():
        m = re.match(r"([^:]+):\s+(\d+)(?:\s+(\w+))?", line)
        if not m:
            continue
        name, value, unit = m.group(1), int(m.group(2)), m.group(3)
        if unit == "kB":
            value *= 1024
        fields[name] = value
    return fields


def read_int(path: Path) -> int:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return 0


def hugepage_size_from_dirname(name: str) -> int | None:
    """`hugepages-2048kB` -> 2048 * 1024 bytes."""
    m = re.match(r"hugepages-(\d+)kB$", name)
    return int(m.group(1)) * 1024 if m else None


def collect_hugepages(root: Path = HUGEPAGES_ROOT) -> list[dict]:
    """Read all configured hugepage sizes from sysfs.

    Each entry: {size, nr, free, resv, surplus, overcommit}. Counts are
    in pages; multiply by `size` for bytes.
    """
    if not root.is_dir():
        return []
    pools = []
    for entry in sorted(root.iterdir(), key=lambda p: hugepage_size_from_dirname(p.name) or 0):
        size = hugepage_size_from_dirname(entry.name)
        if size is None:
            continue
        pools.append({
            "size": size,
            "nr": read_int(entry / "nr_hugepages"),
            "free": read_int(entry / "free_hugepages"),
            "resv": read_int(entry / "resv_hugepages"),
            "surplus": read_int(entry / "surplus_hugepages"),
            "overcommit": read_int(entry / "nr_overcommit_hugepages"),
        })
    return pools


def collect_numa_hugepages(root: Path = NUMA_ROOT) -> dict[int, list[dict]]:
    """Per-NUMA-node hugepage stats: {node_id: [pool, ...]}."""
    if not root.is_dir():
        return {}
    nodes: dict[int, list[dict]] = {}
    for entry in sorted(root.iterdir()):
        m = re.match(r"node(\d+)$", entry.name)
        if not m:
            continue
        node_id = int(m.group(1))
        hp_dir = entry / "hugepages"
        if not hp_dir.is_dir():
            continue
        pools = []
        for hp in sorted(hp_dir.iterdir(), key=lambda p: hugepage_size_from_dirname(p.name) or 0):
            size = hugepage_size_from_dirname(hp.name)
            if size is None:
                continue
            # Per-node sysfs exposes fewer fields than the global pool.
            pools.append({
                "size": size,
                "nr": read_int(hp / "nr_hugepages"),
                "free": read_int(hp / "free_hugepages"),
                "surplus": read_int(hp / "surplus_hugepages"),
            })
        nodes[node_id] = pools
    return nodes


def print_free(mem: dict[str, int]) -> None:
    total = mem.get("MemTotal", 0)
    free = mem.get("MemFree", 0)
    available = mem.get("MemAvailable", 0)
    buffers = mem.get("Buffers", 0)
    cached = mem.get("Cached", 0) + mem.get("SReclaimable", 0) - mem.get("Shmem", 0)
    shared = mem.get("Shmem", 0)
    used = total - free - buffers - cached
    swap_total = mem.get("SwapTotal", 0)
    swap_free = mem.get("SwapFree", 0)
    swap_used = swap_total - swap_free

    header = f"{'':8}{'total':>12}{'used':>12}{'free':>12}{'shared':>12}{'buff/cache':>14}{'available':>14}"
    print("Memory")
    print(header)
    print(f"{'Mem:':8}{human(total):>12}{human(used):>12}{human(free):>12}"
          f"{human(shared):>12}{human(buffers + cached):>14}{human(available):>14}")
    print(f"{'Swap:':8}{human(swap_total):>12}{human(swap_used):>12}{human(swap_free):>12}")
    print()


def print_hugetlb(pools: list[dict]) -> None:
    print("HugeTLB Pages (explicit hugepage pools)")
    if not pools:
        print("  (no hugepage sizes configured)")
        print()
        return
    cols = ("Size", "Total", "Free", "Rsvd", "Surplus", "Overcmt",
            "Used", "Mem Used", "Mem Free")
    widths = (11, 9, 9, 9, 9, 9, 9, 13, 13)
    print("  " + "".join(f"{c:>{w}}" for c, w in zip(cols, widths)))

    total_bytes = 0
    used_bytes = 0
    free_bytes = 0
    for p in pools:
        # `nr_hugepages` is the persistent pool; surplus pages are on top.
        total_pages = p["nr"] + p["surplus"]
        used_pages = total_pages - p["free"]
        total_bytes += total_pages * p["size"]
        used_bytes += used_pages * p["size"]
        free_bytes += p["free"] * p["size"]
        row = (
            human(p["size"]),
            str(p["nr"]),
            str(p["free"]),
            str(p["resv"]),
            str(p["surplus"]),
            str(p["overcommit"]),
            str(used_pages),
            human(used_pages * p["size"]),
            human(p["free"] * p["size"]),
        )
        print("  " + "".join(f"{v:>{w}}" for v, w in zip(row, widths)))
    print(f"  Pool total: {human(total_bytes)}  "
          f"used: {human(used_bytes)}  free: {human(free_bytes)}")
    print()


def print_numa(nodes: dict[int, list[dict]]) -> None:
    if not nodes:
        return
    print("HugeTLB Pages per NUMA node")
    cols = ("Size", "Total", "Free", "Surplus", "Used", "Mem Used", "Mem Free")
    widths = (10, 8, 8, 8, 8, 12, 12)
    for node_id, pools in sorted(nodes.items()):
        print(f"  node{node_id}")
        if not pools:
            print("    (no hugepages)")
            continue
        print("    " + "".join(f"{c:>{w}}" for c, w in zip(cols, widths)))
        for p in pools:
            total_pages = p["nr"] + p["surplus"]
            used_pages = total_pages - p["free"]
            row = (
                human(p["size"]),
                str(p["nr"]),
                str(p["free"]),
                str(p["surplus"]),
                str(used_pages),
                human(used_pages * p["size"]),
                human(p["free"] * p["size"]),
            )
            print("    " + "".join(f"{v:>{w}}" for v, w in zip(row, widths)))
    print()


def categorize_vma(path: str, perms: str) -> str:
    """Classify a VMA by its /proc/*/smaps pathname and permission string.

    Returns one of: heap, stack, code, file-data, anon, shmem, hugetlb,
    vdso, other. 'code' means file-backed executable (main binary + shared
    libs). 'file-data' is a non-executable file mapping (rodata, mmap'd
    data file, etc.).
    """
    if "s" in perms and path.startswith("/") and "hugepages" not in path and "/dev/hugepages" not in path:
        pass  # shared file mapping — falls through to file-data/code below
    if path == "[heap]":
        return "heap"
    if path.startswith("[stack"):
        return "stack"
    if path in ("[vdso]", "[vvar]", "[vsyscall]", "[uprobes]", "[sigpage]"):
        return "vdso"
    if path.startswith("/dev/hugepages") or path.startswith("/anon_hugepage") or "/memfd:" in path and "hugetlb" in path:
        return "hugetlb"
    if path.startswith("["):
        return "other"
    if path:
        return "code" if "x" in perms else "file-data"
    return "shmem" if "s" in perms else "anon"


def parse_smaps(pid: str) -> tuple[list[dict] | None, str | None]:
    """Parse /proc/<pid>/smaps. Returns (entries, error).

    error is None on success, "gone" if the process vanished or is a
    kernel thread (no smaps), or "denied" on permission failure. Kernel
    threads do not have smaps at all — they're not 'unreadable', they
    simply have no user memory to show.
    """
    smaps = PROC / pid / "smaps"
    try:
        text = smaps.read_text()
    except FileNotFoundError:
        return None, "gone"
    except PermissionError:
        return None, "denied"
    except OSError:
        return None, "gone"

    entries: list[dict] = []
    current: dict | None = None
    for line in text.splitlines():
        m = SMAPS_HEADER_RE.match(line)
        if m:
            if current is not None:
                entries.append(current)
            path = (m.group("path") or "").strip()
            perms = m.group("perms")
            current = {"perms": perms, "path": path,
                       "category": categorize_vma(path, perms)}
            for attr in SMAPS_INT_ATTRS:
                current[attr] = 0
        elif current is not None:
            name, _, rest = line.partition(":")
            attr = SMAPS_FIELDS.get(name)
            if attr is None:
                continue
            parts = rest.split()
            if len(parts) >= 2 and parts[1] == "kB":
                current[attr] = int(parts[0]) * 1024
    if current is not None:
        entries.append(current)
    return entries, None


def read_process_name(pid: str) -> str:
    """Full command line with args, fall back to comm when cmdline is empty."""
    try:
        raw = (PROC / pid / "cmdline").read_bytes()
    except OSError:
        raw = b""
    if raw:
        # cmdline is NUL-separated and ends with a trailing NUL. Strip
        # the path from argv[0] so `/usr/lib/firefox/firefox-bin -foo`
        # becomes `firefox-bin -foo`.
        parts = raw.rstrip(b"\x00").split(b"\x00")
        parts[0] = parts[0].rsplit(b"/", 1)[-1]
        return b" ".join(parts).decode("utf-8", errors="replace")
    try:
        return (PROC / pid / "comm").read_text().strip()
    except OSError:
        return "?"


def truncate(s: str, width: int) -> str:
    """Truncate to width chars, using `…` as the overflow marker."""
    if len(s) <= width:
        return s
    return s[:width - 1] + "…"


# Container detection. The kernel exposes no "this process is in a
# container" bit, so we reconstruct the grouping from two signals:
#   1. /proc/<pid>/ns/pid — PID namespace. Every container gets its own,
#      so processes sharing an inode are in the same container.
#   2. /proc/<pid>/cgroup — the cgroup path usually encodes the runtime
#      (docker, podman, kubepods, lxc, nspawn, ...) and a container id.
# We never read cgroup memory accounting; per-container numbers come
# from summing our own per-process smaps data.

# Ordered: most specific patterns first so kubepods wins over the inner
# runc scope it wraps.
CGROUP_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"/kubepods[.\-/].*?(?:pod)?([0-9a-f]{8}[-_][0-9a-f]{4,}[^/]*)"), "k8s"),
    (re.compile(r"/kubepods[.\-/]"), "k8s"),
    (re.compile(r"docker[-/]([0-9a-f]{12,64})"), "docker"),
    (re.compile(r"libpod[-/]([0-9a-f]{12,64})"), "podman"),
    (re.compile(r"crio[-/]([0-9a-f]{12,64})"), "crio"),
    (re.compile(r"containerd[-/]([0-9a-f]{12,64})"), "containerd"),
    (re.compile(r"/lxc(?:\.payload)?[./]([^/]+)"), "lxc"),
    (re.compile(r"machine-([^./]+)\.scope"), "nspawn"),
]

_HOST_NS_CACHE: list[int | None] = []


def pid_namespace_inode(pid: str) -> int | None:
    """Return the nsfs inode of /proc/<pid>/ns/pid, or None if unreadable.

    The symlink target format is ``pid:[4026531836]``; the bracketed
    number is the namespace id (the nsfs inode).
    """
    try:
        target = os.readlink(f"/proc/{pid}/ns/pid")
    except OSError:
        return None
    try:
        return int(target.rsplit("[", 1)[1].rstrip("]"))
    except (ValueError, IndexError):
        return None


def host_pid_namespace() -> int | None:
    """The PID namespace inode of the memsleuth process itself.

    Cached because it's invariant for the run. We use /proc/self because
    /proc/1 is often unreadable for non-root users, but /proc/self is
    always readable by us.
    """
    if not _HOST_NS_CACHE:
        _HOST_NS_CACHE.append(pid_namespace_inode("self"))
    return _HOST_NS_CACHE[0]


def read_cgroup_info(pid: str) -> tuple[list[str], str | None]:
    """Parse /proc/<pid>/cgroup once. Return (all_paths, primary_path).

    all_paths is every hierarchy's path (v2 unified, v1 controllers,
    and named v1 like ``name=systemd`` or ``name=weka``). Container
    runtimes sometimes park their identity on a named v1 hierarchy
    while the memory/pids/unified controllers point at whatever
    wrapper service hosts them (e.g. a Weka box pins its container
    id on ``name=weka:/container/weka/default3`` while memory, pids,
    and the v2 unified entry all read ``/system.slice/weka-agent.service``).
    We have to search every line or we miss the container.

    primary_path is the v2 unified path if present, else the v1
    memory/pids controller — used only for the structural
    system.slice / user.slice / system buckets after container
    detection has had a chance.
    """
    try:
        text = (PROC / pid / "cgroup").read_text()
    except OSError:
        return [], None
    all_paths: list[str] = []
    unified: str | None = None
    v1_fallback: str | None = None
    for line in text.splitlines():
        parts = line.strip().split(":", 2)
        if len(parts) != 3:
            continue
        hier, ctrl, path = parts
        all_paths.append(path)
        if hier == "0" and ctrl == "":
            unified = path
        elif ctrl in ("memory", "pids") and v1_fallback is None:
            v1_fallback = path
    return all_paths, unified or v1_fallback


def container_label_from_cgroup(path: str | None) -> str | None:
    if not path:
        return None
    for pattern, kind in CGROUP_PATTERNS:
        m = pattern.search(path)
        if m:
            ident = (m.group(1) if m.groups() else "")[:12]
            return f"{kind}:{ident}" if ident else kind
    return None


# Paths under /container/<runtime>/<id>/... belong to a custom container
# framework (Weka's layout on the boxes we target). First two segments
# after /container identify the container; deeper paths (sub-cgroups
# inside the container) still map to the same bucket.
CONTAINER_SLOT_RE = re.compile(r"^/container/([^/]+)(?:/([^/]+))?")


def classify_container(pid: str) -> tuple[str, str]:
    """Return (group_key, label).

    Buckets in priority order:
      1. /container/<runtime>/<id> — the Weka-style custom container layout.
         Labelled '<runtime>:<id>'. Sub-cgroups inside the container collapse
         into the same bucket.
      2. Known runtime cgroup patterns (docker, podman, kubepods, crio,
         containerd, lxc, nspawn) — labelled '<runtime>:<id>'.
      3. /system.slice/* — one bucket, labelled 'system.slice'.
      4. /user.slice/*   — one bucket, labelled 'user.slice' (all user UIDs).
      5. Everything else (/, /init.scope, /system, ...) — 'system'.

    A separate PID namespace alone is not treated as a container: browser
    sandboxes (Firefox, Chromium) each get their own pid ns and would
    flood the view.

    The container search walks every cgroup hierarchy (including named
    v1 like ``name=weka``) because runtimes such as Weka's container
    framework record the container id on a named hierarchy while the
    memory / pids / v2-unified lines point at the host wrapper
    service. Matching only the unified line misses those.
    """
    all_paths, primary = read_cgroup_info(pid)
    if not all_paths:
        return ("system", "system")

    # 1. Container-style layouts in ANY hierarchy (named v1 included).
    for p in all_paths:
        m = CONTAINER_SLOT_RE.match(p)
        if m:
            runtime, ident = m.group(1), m.group(2)
            label = f"{runtime}:{ident}" if ident else f"container:{runtime}"
            return (label, label)

    # 2. Runtime patterns in any hierarchy.
    for p in all_paths:
        cg_label = container_label_from_cgroup(p)
        if cg_label:
            return (cg_label, cg_label)

    # 3-5. Structural buckets from the primary path only.
    if primary == "/system.slice" or (primary and primary.startswith("/system.slice/")):
        return ("system.slice", "system.slice")
    if primary == "/user.slice" or (primary and primary.startswith("/user.slice/")):
        return ("user.slice", "user.slice")

    return ("system", "system")


SEGMENT_MIN_SHARED = 64 * 1024  # only list segments sharing ≥64 KiB


def aggregate_process(entries: list[dict], keep_segments: bool = False) -> dict:
    """Roll VMA entries up into a per-process summary.

    When keep_segments is True, also return a `segments` list of the
    top shared VMAs (sorted descending by shared bytes).
    """
    agg: dict = {
        "rss": 0, "pss": 0, "shared_rss": 0,
        "code_rss": 0, "heap_rss": 0, "stack_rss": 0,
        "data_rss": 0, "shmem_rss": 0, "file_data_rss": 0,
        "thp_code": 0, "thp_data": 0,
        "hugetlb_priv": 0, "hugetlb_shared": 0,
        "swap": 0, "exe_ondisk": 0, "file_ondisk": 0,
    }
    segments: list[dict] = [] if keep_segments else []
    for v in entries:
        agg["rss"] += v["rss"]
        agg["pss"] += v["pss"]
        agg["swap"] += v["swap"]
        shared = v["shared_clean"] + v["shared_dirty"]
        agg["shared_rss"] += shared
        cat = v["category"]
        # For file-backed VMAs, Size - Rss counts bytes not in RAM that
        # would require reading the backing file — treat this as
        # "swapped to the exe/data file" in the reporting sense.
        if cat == "code":
            agg["code_rss"] += v["rss"]
            agg["thp_code"] += v["anon_thp"] + v["file_pmd"]
            agg["exe_ondisk"] += max(0, v["size"] - v["rss"])
        elif cat == "heap":
            agg["heap_rss"] += v["rss"]
            agg["thp_data"] += v["anon_thp"]
        elif cat == "stack":
            agg["stack_rss"] += v["rss"]
            agg["thp_data"] += v["anon_thp"]
        elif cat == "anon":
            agg["data_rss"] += v["rss"]
            agg["thp_data"] += v["anon_thp"]
        elif cat == "shmem":
            agg["shmem_rss"] += v["rss"]
            agg["thp_data"] += v["shmem_pmd"]
        elif cat == "file-data":
            agg["file_data_rss"] += v["rss"]
            agg["thp_data"] += v["file_pmd"] + v["shmem_pmd"]
            agg["file_ondisk"] += max(0, v["size"] - v["rss"])
        agg["hugetlb_priv"] += v["hugetlb_priv"]
        agg["hugetlb_shared"] += v["hugetlb_shared"]
        if keep_segments and shared >= SEGMENT_MIN_SHARED:
            segments.append({
                "path": v["path"] or "(anon)",
                "perms": v["perms"],
                "category": v["category"],
                "rss": v["rss"],
                "pss": v["pss"],
                "shared": shared,
            })
    if keep_segments:
        # Merge segments with the same path + perms — a single file is
        # often split across several VMAs (rodata, code) at different
        # offsets, and the user wants one line per logical region.
        merged: dict[tuple[str, str], dict] = {}
        for s in segments:
            key = (s["path"], s["perms"])
            if key in merged:
                m = merged[key]
                m["rss"] += s["rss"]
                m["pss"] += s["pss"]
                m["shared"] += s["shared"]
            else:
                merged[key] = dict(s)
        agg["segments"] = sorted(merged.values(),
                                  key=lambda s: s["shared"], reverse=True)
    return agg


def collect_process_details(keep_segments: bool = False) -> tuple[list[dict], int, int]:
    """Walk /proc scanning smaps. Returns (rows, denied, kernel_threads)."""
    rows: list[dict] = []
    denied = 0
    gone = 0
    for entry in PROC.iterdir():
        if not entry.name.isdigit():
            continue
        entries, err = parse_smaps(entry.name)
        if err == "denied":
            denied += 1
            continue
        if err == "gone" or entries is None:
            gone += 1
            continue
        agg = aggregate_process(entries, keep_segments=keep_segments)
        if agg["rss"] == 0 and agg["hugetlb_priv"] == 0 and agg["hugetlb_shared"] == 0:
            continue
        agg["pid"] = int(entry.name)
        agg["name"] = read_process_name(entry.name)
        key, label = classify_container(entry.name)
        agg["container_key"] = key
        agg["container_label"] = label
        rows.append(agg)
    return rows, denied, gone


SEGMENTS_PER_PROCESS = 10


def _shorten_path(path: str, width: int) -> str:
    if len(path) <= width:
        return path
    # Keep the basename and leading `.../` marker.
    return "..." + path[-(width - 3):]


PROC_TABLE_HDR = ("PID", "Command", "RSS", "Code", "Heap", "Stack",
                  "AnonData", "Shared", "Swap", "ExeSwap", "FileSwap",
                  "THP/code", "THP/data", "HugeTLB")
PROC_TABLE_WIDTHS = (8, 44, 12, 12, 12, 12, 12, 12, 12, 12, 12, 12, 12, 12)


def _print_process_table(rows: list[dict], top: int | None,
                          show_segments: bool, indent: str) -> None:
    print(indent + "  ".join(f"{c:<{w}}" if i < 2 else f"{c:>{w}}"
                              for i, (c, w) in enumerate(
                                  zip(PROC_TABLE_HDR, PROC_TABLE_WIDTHS))))
    shown = rows[:top] if top else rows
    for r in shown:
        hugetlb = r["hugetlb_priv"] + r["hugetlb_shared"]
        vals = (
            str(r["pid"]), truncate(r["name"], 44),
            human(r["rss"]), human(r["code_rss"]),
            human(r["heap_rss"]), human(r["stack_rss"]),
            human(r["data_rss"]), human(r["shared_rss"]),
            human(r["swap"]), human(r["exe_ondisk"]),
            human(r["file_ondisk"]),
            human(r["thp_code"]), human(r["thp_data"]),
            human(hugetlb),
        )
        print(indent + "  ".join(f"{v:<{w}}" if i < 2 else f"{v:>{w}}"
                                  for i, (v, w) in enumerate(
                                      zip(vals, PROC_TABLE_WIDTHS))))
        if show_segments:
            _print_segments(r.get("segments", []))
    if top and len(rows) > top:
        print(f"{indent}... {len(rows) - top} more (use --top 0 for all)")


def aggregate_containers(rows: list[dict]) -> list[dict]:
    """Sum per-process stats into per-container totals."""
    fields = ("rss", "code_rss", "heap_rss", "stack_rss", "data_rss",
              "shared_rss", "swap", "exe_ondisk", "file_ondisk",
              "thp_code", "thp_data", "hugetlb_priv", "hugetlb_shared")
    bucket: dict[str, dict] = {}
    for r in rows:
        key = r["container_key"]
        c = bucket.get(key)
        if c is None:
            c = {"key": key, "label": r["container_label"], "procs": 0}
            for f in fields:
                c[f] = 0
            bucket[key] = c
        c["procs"] += 1
        for f in fields:
            c[f] += r[f]
    # Sort: system last (it's usually largest and shadows container
    # differences), containers by RSS descending in front of it.
    # Sort: real containers first (by RSS desc), then the system-level
    # buckets (system.slice, user.slice, system) at the bottom in a
    # stable order so the "containers of interest" lead the table.
    system_rank = {"system.slice": 1, "user.slice": 2, "system": 3}
    return sorted(
        bucket.values(),
        key=lambda c: (system_rank.get(c["key"], 0), -c["rss"]),
    )


def _print_container_summary(containers: list[dict]) -> None:
    print()
    print("Per-container summary (grouped by PID namespace / cgroup)")
    hdr = ("Container", "Procs", "RSS", "Code", "Heap", "Stack",
           "AnonData", "Shared", "Swap", "ExeSwap", "FileSwap",
           "THP/code", "THP/data", "HugeTLB")
    widths = (28, 6, 12, 12, 12, 12, 12, 12, 12, 12, 12, 12, 12, 12)
    print("  " + "  ".join(f"{c:<{w}}" if i == 0 else f"{c:>{w}}"
                            for i, (c, w) in enumerate(zip(hdr, widths))))
    for c in containers:
        hugetlb = c["hugetlb_priv"] + c["hugetlb_shared"]
        vals = (
            truncate(c["label"], 28), str(c["procs"]),
            human(c["rss"]), human(c["code_rss"]),
            human(c["heap_rss"]), human(c["stack_rss"]),
            human(c["data_rss"]), human(c["shared_rss"]),
            human(c["swap"]), human(c["exe_ondisk"]),
            human(c["file_ondisk"]),
            human(c["thp_code"]), human(c["thp_data"]),
            human(hugetlb),
        )
        print("  " + "  ".join(f"{v:<{w}}" if i == 0 else f"{v:>{w}}"
                                for i, (v, w) in enumerate(zip(vals, widths))))


def print_process_details(top: int | None, show_segments: bool,
                           group_by_container: bool) -> None:
    rows, denied, gone = collect_process_details(keep_segments=show_segments)
    if rows:
        rows.sort(key=lambda r: r["rss"], reverse=True)

    containers = aggregate_containers(rows) if rows else []
    # Surface the summary whenever classification split processes across
    # more than one bucket — on a typical host that already yields
    # system.slice / user.slice / system, which users find useful.
    # Always show it when the user asked for the grouped view explicitly.
    has_containers = len(containers) > 1 or group_by_container

    if group_by_container and rows:
        print("Per-process memory detail (grouped by container)")
        for c in containers:
            proc_rows = [r for r in rows if r["container_key"] == c["key"]]
            hugetlb = c["hugetlb_priv"] + c["hugetlb_shared"]
            header_bits = [
                f"procs={c['procs']}",
                f"RSS={human(c['rss'])}",
                f"swap={human(c['swap'])}",
                f"shared={human(c['shared_rss'])}",
            ]
            if hugetlb:
                header_bits.append(f"hugetlb={human(hugetlb)}")
            print(f"\n  [{c['label']}]  " + "  ".join(header_bits))
            _print_process_table(proc_rows, top, show_segments, indent="    ")
    else:
        print("Per-process memory detail")
        if not rows:
            print("  (no processes with user memory found)")
        else:
            _print_process_table(rows, top, show_segments, indent="  ")

    if has_containers:
        _print_container_summary(containers)

    _print_hugetlb_table(rows, top)
    _print_proc_notes(denied, gone)


def _print_segments(segments: list[dict]) -> None:
    if not segments:
        return
    shown = segments[:SEGMENTS_PER_PROCESS]
    # Indented sub-table. `~sharers` is approximate: Rss/Pss — if a page
    # is mapped by N processes its Pss is 1/N of its size, so the ratio
    # approximates the number of processes sharing the VMA.
    seg_hdr = ("RSS", "Shared", "~sharers", "perms", "path")
    seg_widths = (11, 11, 9, 6)  # path is free-form at the end
    print("      " + " ".join(f"{c:>{w}}" for c, w in zip(seg_hdr, seg_widths))
          + "  " + seg_hdr[4])
    for s in shown:
        sharers = round(s["rss"] / s["pss"]) if s["pss"] else 0
        sharers_str = f"~{sharers}x" if sharers else "?"
        vals = (human(s["rss"]), human(s["shared"]), sharers_str, s["perms"])
        print("      " + " ".join(f"{v:>{w}}" for v, w in zip(vals, seg_widths))
              + "  " + _shorten_path(s["path"], 80))
    if len(segments) > SEGMENTS_PER_PROCESS:
        print(f"      ... {len(segments) - SEGMENTS_PER_PROCESS} more segments")


def _print_hugetlb_table(rows: list[dict], top: int | None) -> None:
    hugetlb_rows = [r for r in rows if r["hugetlb_priv"] or r["hugetlb_shared"]]
    if not hugetlb_rows:
        return
    print()
    print("HugeTLB (hugetlbfs) users — Private / Shared")
    hugetlb_rows.sort(key=lambda r: r["hugetlb_priv"] + r["hugetlb_shared"],
                      reverse=True)
    hdr = ("PID", "Command", "Private", "Shared", "Total")
    widths = (8, 44, 14, 14, 14)
    print("  " + "  ".join(f"{c:<{w}}" if i < 2 else f"{c:>{w}}"
                            for i, (c, w) in enumerate(zip(hdr, widths))))
    shown = hugetlb_rows[:top] if top else hugetlb_rows
    for r in shown:
        total = r["hugetlb_priv"] + r["hugetlb_shared"]
        vals = (str(r["pid"]), truncate(r["name"], 44),
                human(r["hugetlb_priv"]), human(r["hugetlb_shared"]),
                human(total))
        print("  " + "  ".join(f"{v:<{w}}" if i < 2 else f"{v:>{w}}"
                                for i, (v, w) in enumerate(zip(vals, widths))))


def _print_proc_notes(denied: int, gone: int) -> None:
    notes = []
    if denied:
        notes.append(f"{denied} processes denied (need root or CAP_SYS_PTRACE)")
    if gone:
        notes.append(f"{gone} kernel threads / exited processes skipped")
    if notes:
        print()
        print("  note: " + "; ".join(notes))
    print()


def print_thp(mem: dict[str, int]) -> None:
    """Transparent/implicit huge pages — already counted inside MemUsed."""
    anon = mem.get("AnonHugePages", 0)
    shmem = mem.get("ShmemHugePages", 0)
    filehp = mem.get("FileHugePages", 0)
    shmem_pmd = mem.get("ShmemPmdMapped", 0)
    file_pmd = mem.get("FilePmdMapped", 0)
    if not any((anon, shmem, filehp, shmem_pmd, file_pmd)):
        return
    print("Transparent Huge Pages (already counted in Mem used)")
    print(f"  AnonHugePages:    {human(anon):>12}  (anonymous THP)")
    print(f"  ShmemHugePages:   {human(shmem):>12}  (shmem-backed THP)")
    print(f"  FileHugePages:    {human(filehp):>12}  (file-backed THP)")
    if shmem_pmd:
        print(f"  ShmemPmdMapped:   {human(shmem_pmd):>12}")
    if file_pmd:
        print(f"  FilePmdMapped:    {human(file_pmd):>12}")
    print()


def print_directmap(mem: dict[str, int]) -> None:
    keys = [k for k in mem if k.startswith("DirectMap")]
    if not keys:
        return
    print("Kernel Direct Map (physical memory mapped by page size)")
    for k in sorted(keys, key=lambda x: ("4k", "2M", "1G").index(x.replace("DirectMap", "")) if x.replace("DirectMap", "") in ("4k", "2M", "1G") else 99):
        print(f"  {k + ':':16}{human(mem[k]):>12}")
    print()


FIELDS_HELP = """\
Field reference for memsleuth output
====================================

Top summary (free(1) style)
---------------------------
  total       MemTotal — physical RAM installed.
  used        total - free - buff/cache  (memory held by processes + kernel).
  free        MemFree — genuinely unused pages.
  shared      Shmem — tmpfs and shared anonymous mappings.
  buff/cache  Buffers + Cached + SReclaimable - Shmem — pages the kernel can drop under pressure.
  available   MemAvailable — kernel's estimate of what a new workload can obtain without swapping.
  Swap used   SwapTotal - SwapFree.

HugeTLB Pages (explicit hugepage pool, per size)
------------------------------------------------
  Size        Hugepage size for this row (2 MiB, 1 GiB, ...).
  Total       /sys/.../nr_hugepages — the persistent pool.
  Free        /sys/.../free_hugepages — pool pages not currently in use.
  Rsvd        /sys/.../resv_hugepages — reserved for mappings that faulted a VMA but haven't yet
              touched the page.
  Surplus     /sys/.../surplus_hugepages — pages allocated on demand above the persistent pool
              (returned to the system once no longer used).
  Overcmt     /sys/.../nr_overcommit_hugepages — the cap on Surplus.
  Used        (Total + Surplus) - Free.
  Mem Used    Used * Size, in bytes.
  Mem Free    Free * Size, in bytes.

Transparent Huge Pages (implicit THP, system-wide)
--------------------------------------------------
  AnonHugePages    /proc/meminfo — anon memory backed by THP.
  ShmemHugePages   shmem/tmpfs backed by THP.
  FileHugePages    file-backed THP (CONFIG_READ_ONLY_THP_FOR_FS).
  These are already counted inside Mem used — they describe *how* that memory is mapped, not any
  additional consumption.

Per-process memory detail (--procs)
-----------------------------------
  PID         Process ID.
  Command     /proc/<pid>/cmdline; argv[0] reduced to its basename. Truncated to 44 chars with '…'
              appended when longer.
  RSS         Total resident set size across all VMAs.
  Code        RSS of file-backed executable mappings (main binary plus .so libraries — any VMA with
              'x' permission and a path).
  Heap        RSS of the [heap] VMA.
  Stack       RSS of [stack] / [stack:tid] VMAs.
  AnonData    RSS of anonymous mappings (bss, mmap scratch, allocator arenas, JIT code). Excludes
              heap and stack.
  Shared      Shared_Clean + Shared_Dirty summed across VMAs — memory this process shares with at
              least one other process.
  Swap        Anon pages pushed to the swap device (smaps Swap field). Includes COW'd pages from
              private file mappings, which become anonymous after the first write.
  ExeSwap     File-backed executable mappings not in RAM right now (Size - Rss). Would re-read from
              the binary/.so on access. Includes pages that were never faulted in.
  FileSwap    Same as ExeSwap but for non-exec file mappings. Large values are common for mmap'd
              fonts, locale-archive, memory-mapped databases.
  THP/code    AnonHugePages + FilePmdMapped on executable VMAs. Non-zero means THP is backing code
              pages.
  THP/data    AnonHugePages + FilePmdMapped + ShmemPmdMapped on non-exec VMAs.
  HugeTLB     Private_Hugetlb + Shared_Hugetlb — hugetlbfs pages this process has mapped.

Per-process shared segments (--shared)
--------------------------------------
  RSS         Per-segment resident size (merged across VMAs that share the same path and perms).
  Shared      Shared_Clean + Shared_Dirty for this segment.
  ~sharers    Approximate number of processes sharing this segment, computed as round(Rss / Pss).
              Displayed as '~Nx'. Shows '?' when Pss is 0.
  perms       VMA perms (r-xp = private exec, r--p = private rodata, r--s = shared read,
              rw-p = private read-write).
  path        Backing file or [heap] / [stack] / (anon) / /memfd:...

Per-container summary (automatic when > 1 bucket exists; always with --containers)
-----------------------------------------------------------------------------------
Each process lands in exactly one bucket based on its cgroup path. Priority order:

  1. /container/<runtime>/<id>[/...]   → '<runtime>:<id>'   (custom cgroup-based layouts such
                                                              as Weka's /container/weka/default0)
  2. Known runtime patterns in the cgroup (docker, podman/libpod, kubepods, crio, containerd,
     lxc, systemd-nspawn)              → '<runtime>:<id>'
  3. /system.slice/*                   → 'system.slice'     (one bucket; all systemd services)
  4. /user.slice/*                     → 'user.slice'       (one bucket; all user sessions)
  5. everything else (/, /init.scope,  → 'system'
     /system, unreadable cgroup, ...)

A separate PID namespace alone is not treated as a container — browser sandboxes each create their
own pid ns and would otherwise flood the view.

The summary prints automatically whenever at least two buckets have processes. Run with
--containers to also group the per-process listing under each bucket's header.

  Container      Bucket label as listed above.
  Procs          Number of processes aggregated into this row.
  RSS ... HugeTLB  Same semantics as the per-process columns, summed across the bucket's processes.
                   Memory numbers come from smaps (not cgroup memory.stat) so they are consistent with
                   the per-process view and do not rely on any in-container accounting.

NUMA hugepage breakdown (--numa)
--------------------------------
  Same columns as the HugeTLB table but pulled from /sys/devices/system/node/nodeN. The kernel does
  not expose Rsvd or Overcmt per node.

Kernel Direct Map
-----------------
  DirectMap4k/2M/1G — how the kernel's linear mapping of physical RAM is split across 4 KiB / 2 MiB
  / 1 GiB page sizes. Growing DirectMap4k over uptime usually means direct-map fragmentation
  (module loads, eBPF JIT, permission changes) which costs TLB performance. The numbers describe
  *how* RAM is mapped into kernel virtual space — they aren't a separate consumer of RAM.

Notes on attribution limits
---------------------------
- Per-process reporting parses /proc/<pid>/smaps and needs permission (root or CAP_SYS_PTRACE) to
  read other users' processes.
- Kernel threads have no smaps and are reported separately from real permission failures.
- Size - Rss conflates 'evicted' with 'never faulted'; treat ExeSwap / FileSwap as upper bounds on
  actual eviction.
"""


def main(argv: list[str] | None = None) -> int:
    # Assume a 120-column terminal. argparse's default is the actual
    # terminal width via $COLUMNS; forcing 120 gives consistent,
    # readable help when piped or run in narrow panes.
    def formatter(prog: str) -> argparse.HelpFormatter:
        return argparse.HelpFormatter(prog, max_help_position=28, width=120)

    ap = argparse.ArgumentParser(
        description="Show Linux memory with a full hugepages breakdown.",
        epilog="For a full explanation of every column, run `memsleuth --help-fields`.",
        formatter_class=formatter,
    )
    ap.add_argument("--numa", action="store_true",
                    help="break down hugepages per NUMA node")
    ap.add_argument("--procs", action="store_true",
                    help="show per-process memory breakdown (RSS, code, heap, stack, THP, hugetlb)")
    ap.add_argument("--shared", action="store_true",
                    help="list each process's top shared segments (implies --procs)")
    ap.add_argument("--containers", action="store_true",
                    help="group the per-process listing by container (implies --procs); "
                         "the container summary is always printed when containers are detected")
    ap.add_argument("--top", type=int, default=15, metavar="N",
                    help="with --procs, show only top N processes "
                         "(per container when --containers is set; 0 = all)")
    ap.add_argument("--no-thp", action="store_true",
                    help="hide transparent hugepage counters")
    ap.add_argument("--no-directmap", action="store_true",
                    help="hide kernel DirectMap breakdown")
    ap.add_argument("--help-fields", action="store_true",
                    help="print a detailed explanation of every output field and exit")
    args = ap.parse_args(argv)

    if args.help_fields:
        sys.stdout.write(FIELDS_HELP)
        return 0

    if not MEMINFO.exists():
        print(f"error: {MEMINFO} not found — not a Linux system?", file=sys.stderr)
        return 1

    mem = parse_meminfo()
    print_free(mem)
    print_hugetlb(collect_hugepages())
    if args.numa:
        print_numa(collect_numa_hugepages())
    if not args.no_thp:
        print_thp(mem)
    if args.procs or args.shared or args.containers:
        print_process_details(args.top or None,
                               show_segments=args.shared,
                               group_by_container=args.containers)
    if not args.no_directmap:
        print_directmap(mem)
    return 0


if __name__ == "__main__":
    sys.exit(main())
