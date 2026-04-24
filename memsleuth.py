#!/usr/bin/env python3
"""memsleuth - show Linux memory usage with a full hugepages breakdown.

Reads /proc/meminfo for overall memory (like `free`) and walks
/sys/kernel/mm/hugepages/ to report every configured hugepage size with
total/free/reserved/surplus counts. Optionally breaks the hugepage pools
down per NUMA node.
"""

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Pattern, Tuple, Union

HUGEPAGES_ROOT = Path("/sys/kernel/mm/hugepages")
NUMA_ROOT = Path("/sys/devices/system/node")
MEMINFO = Path("/proc/meminfo")
BUDDYINFO = Path("/proc/buddyinfo")
PAGETYPEINFO = Path("/proc/pagetypeinfo")
PROC = Path("/proc")

# Migration-type pools from which a 2 MiB (or larger) allocation can
# usually succeed without compaction. Unmovable blocks of the right
# size *might* satisfy a hugepage request but may require migrating
# in-use kernel pages out of the way, which can fail.
MOVABLE_MTYPES = {"Movable", "Reclaimable", "CMA"}

_BASE_PAGE_SIZE_CACHE: List[int] = []

_ONLINE_NUMA_NODES_CACHE: List[List[int]] = []

PROC_HP_FIELDS = ("AnonHugePages", "ShmemPmdMapped", "FilePmdMapped",
                  "Shared_Hugetlb", "Private_Hugetlb")

SMAPS_HEADER_RE = re.compile(
    r"^(?P<start>[0-9a-f]+)-[0-9a-f]+\s+(?P<perms>\S+)\s+\S+\s+\S+\s+\S+(?:\s+(?P<path>.*))?$"
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


def parse_meminfo(path: Path = MEMINFO) -> Dict[str, int]:
    """Return /proc/meminfo as a dict of name -> bytes."""
    fields: Dict[str, int] = {}
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


def hugepage_size_from_dirname(name: str) -> Optional[int]:
    """`hugepages-2048kB` -> 2048 * 1024 bytes."""
    m = re.match(r"hugepages-(\d+)kB$", name)
    return int(m.group(1)) * 1024 if m else None


def collect_hugepages(root: Path = HUGEPAGES_ROOT) -> List[dict]:
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


def base_page_size() -> int:
    """Kernel base page size (usually 4096). Cached."""
    if not _BASE_PAGE_SIZE_CACHE:
        try:
            _BASE_PAGE_SIZE_CACHE.append(int(os.sysconf("SC_PAGE_SIZE")))
        except (ValueError, OSError):
            _BASE_PAGE_SIZE_CACHE.append(4096)
    return _BASE_PAGE_SIZE_CACHE[0]


def _buddy_count(tok: str) -> int:
    """Parse one free-count cell from buddyinfo / pagetypeinfo.

    The kernel caps very large free counts with a '>' prefix (e.g.
    '>100000' — see MAX_LINE_LEN handling in mm/vmstat.c) to keep the
    column width sane. Treat that as the bare number; it's a lower
    bound, which is still sound for 'how many hugepages could I get'.
    """
    if tok.startswith(">"):
        tok = tok[1:]
    try:
        return int(tok)
    except ValueError:
        return 0


def parse_buddyinfo() -> Dict[int, Dict[str, List[int]]]:
    """Parse /proc/buddyinfo: ``{node_id: {zone: [count_at_order_0, ...]}}``.

    Each row's columns are the per-order free block counts in the
    buddy allocator. Total across zones and migration types is what
    ``MemFree`` in /proc/meminfo would tell you; the order-by-order
    breakdown is what matters for hugepage availability.
    """
    try:
        text = BUDDYINFO.read_text()
    except OSError:
        return {}
    result: Dict[int, Dict[str, List[int]]] = {}
    for line in text.splitlines():
        m = re.match(r"Node\s+(\d+),\s+zone\s+(\S+)\s+(.+)$", line)
        if not m:
            continue
        node = int(m.group(1))
        zone = m.group(2)
        counts = [_buddy_count(x) for x in m.group(3).split()]
        result.setdefault(node, {})[zone] = counts
    return result


def parse_pagetypeinfo() -> Dict[int, Dict[str, Dict[str, List[int]]]]:
    """Parse /proc/pagetypeinfo's per-migration-type free-area section.

    Returns ``{node: {zone: {migration_type: [counts_by_order]}}}``.
    Migration types are Unmovable / Movable / Reclaimable / HighAtomic /
    CMA / Isolate. The header and the trailing 'Number of blocks' block
    are ignored because their lines don't match the regex.
    """
    try:
        text = PAGETYPEINFO.read_text()
    except OSError:
        return {}
    result: Dict[int, Dict[str, Dict[str, List[int]]]] = {}
    for line in text.splitlines():
        m = re.match(r"Node\s+(\d+),\s+zone\s+(\S+),\s+type\s+(\S+)\s+(.+)$", line)
        if not m:
            continue
        node = int(m.group(1))
        zone = m.group(2)
        mtype = m.group(3)
        counts = [_buddy_count(x) for x in m.group(4).split()]
        result.setdefault(node, {}).setdefault(zone, {})[mtype] = counts
    return result


def parse_pagetypeinfo_blocks() -> Tuple[Dict[int, Dict[str, Dict[str, int]]], int]:
    """Parse /proc/pagetypeinfo's 'Number of blocks type' section.

    Returns ``(blocks, pageblock_size)`` where ``blocks[node][zone][mtype]``
    is the count of pageblocks of that migration type. The pageblock
    size (in bytes) is derived from the file header's ``Pages per
    block: N`` line — typically 2 MiB on x86_64 with 4 KiB base pages.

    For hugepage sizes the buddy allocator can't express (1 GiB), this
    section gives a tighter upper bound than MemFree alone: to allocate
    an N GiB hugepage the kernel needs N GiB / pageblock_size
    consecutive pageblocks that are all migratable. The count is still
    optimistic (we don't know that they're actually contiguous), but
    it's closer to what ``__alloc_contig_pages`` can actually deliver.
    """
    try:
        text = PAGETYPEINFO.read_text()
    except OSError:
        return {}, 0
    blocks: Dict[int, Dict[str, Dict[str, int]]] = {}
    types: Optional[List[str]] = None
    pages_per_block = 0
    for line in text.splitlines():
        m_hdr = re.match(r"Pages per block:\s+(\d+)", line)
        if m_hdr:
            pages_per_block = int(m_hdr.group(1))
            continue
        if line.startswith("Number of blocks type"):
            types = line.split()[4:]
            continue
        if types is None:
            # still inside the free-area section; skip
            continue
        # Block-count rows don't have ", type X" after the zone.
        m = re.match(r"Node\s+(\d+),\s+zone\s+(\S+)\s+(.+)$", line)
        if not m or ", type" in line:
            continue
        counts = [_buddy_count(x) for x in m.group(3).split()[:len(types)]]
        if len(counts) < len(types):
            continue
        node = int(m.group(1))
        zone = m.group(2)
        per_zone = blocks.setdefault(node, {}).setdefault(zone, {})
        for t, c in zip(types, counts):
            per_zone[t] = c
    pageblock_size = pages_per_block * base_page_size() if pages_per_block else 0
    return blocks, pageblock_size


def hugepage_availability_all(
    buddy: Dict[int, Dict[str, List[int]]],
    hugepage_order: int,
) -> Dict[int, Dict[str, int]]:
    """Per-node total free blocks at order ≥ hugepage_order, across all
    zones and migration types. Larger-order blocks contribute multiple
    hugepages (an order-K block splits into ``2**(K - hugepage_order)``).
    """
    result: Dict[int, Dict[str, int]] = {}
    for node, zones in buddy.items():
        total = 0
        max_order = -1
        for counts in zones.values():
            if hugepage_order >= len(counts):
                continue
            for order in range(hugepage_order, len(counts)):
                n = counts[order]
                if n and order > max_order:
                    max_order = order
                total += n * (1 << (order - hugepage_order))
        result[node] = {"all": total, "max_order": max_order}
    return result


def hugepage_availability_safe(
    pagetype: Dict[int, Dict[str, Dict[str, List[int]]]],
    hugepage_order: int,
) -> Dict[int, int]:
    """Per-node count from movable-ish (Movable/Reclaimable/CMA) pools
    only — pages that can be allocated without relocating kernel data.
    Returns {} when pagetypeinfo wasn't readable (needs root)."""
    result: Dict[int, int] = {}
    for node, zones in pagetype.items():
        total = 0
        for per_mtype in zones.values():
            for mtype, counts in per_mtype.items():
                if mtype not in MOVABLE_MTYPES or hugepage_order >= len(counts):
                    continue
                for order in range(hugepage_order, len(counts)):
                    total += counts[order] * (1 << (order - hugepage_order))
        result[node] = total
    return result


def buddy_max_order(buddy: dict) -> int:
    """Highest buddy order exposed by this kernel (MAX_ORDER - 1)."""
    mo = -1
    for zones in buddy.values():
        for counts in zones.values():
            if len(counts) - 1 > mo:
                mo = len(counts) - 1
    return mo


def collect_numa_hugepages(root: Path = NUMA_ROOT) -> Dict[int, List[dict]]:
    """Per-NUMA-node hugepage stats: {node_id: [pool, ...]}."""
    if not root.is_dir():
        return {}
    nodes: Dict[int, List[dict]] = {}
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


def print_free(mem: Dict[str, int]) -> None:
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


def print_hugetlb(pools: List[dict]) -> None:
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


def print_hugepage_capacity(pools: List[dict],
                             numa_hugetlb: Dict[int, List[dict]]) -> None:
    """Per-NUMA "can I allocate a hugepage right now?" table.

    Columns:
      Pool free/total - persistent pool (from /sys/.../hugepages-*kB/).
      Buddy safe      - free pages in Movable/Reclaimable/CMA pools at
                        order ≥ hp_order, allocatable without
                        compacting kernel data. Requires
                        /proc/pagetypeinfo (root-only); shows 'needs
                        root' otherwise.
      Buddy max       - free pages across all migration types. From
                        /proc/buddyinfo (world-readable).

    Sizes the buddy allocator can't represent (typically 1 GiB, since
    MAX_ORDER is usually 10 → 4 MiB cap) show 'pool only' — these
    come from the persistent pool or hugetlb_cma only, not from
    runtime allocation.
    """
    if not pools:
        return
    buddy = parse_buddyinfo()
    pagetype = parse_pagetypeinfo()
    _, pageblock_size = parse_pagetypeinfo_blocks()
    if not pageblock_size and pools:
        # Fallback: the smallest configured hugepage equals the pageblock
        # on kernels we care about (x86_64 with HUGETLB_PAGE_ORDER == 9).
        pageblock_size = min(p["size"] for p in pools)
    base = base_page_size()
    max_order = buddy_max_order(buddy) if buddy else -1
    # Order of a pageblock; used to scale free-block counts down to the
    # unit we care about for buddy-inexpressible hugepage sizes.
    pageblock_order = -1
    if base and pageblock_size:
        ratio = pageblock_size // base
        if ratio > 0 and (ratio & (ratio - 1)) == 0:
            pageblock_order = ratio.bit_length() - 1

    pool_by_size: Dict[Tuple[int, int], dict] = {}
    for node_id, node_pools in numa_hugetlb.items():
        for p in node_pools:
            pool_by_size[(p["size"], node_id)] = p

    print("Hugepage allocation capacity")
    print("  (pool = persistent hugepage pool; buddy = free blocks big enough in the page allocator)")
    hdr = ("Size", "Node", "Pool free", "Pool total", "Buddy safe", "Buddy max")
    widths = (10, 8, 11, 11, 22, 22)
    print("  " + "  ".join(f"{h:<{w}}" if i < 2 else f"{h:>{w}}"
                            for i, (h, w) in enumerate(zip(hdr, widths))))

    nodes_present = sorted(
        numa_hugetlb.keys() | buddy.keys() | (pagetype.keys() if pagetype else set())
    )
    if not nodes_present:
        nodes_present = [0]

    for pool in pools:
        hp_size = pool["size"]
        ratio = hp_size // base if base else 0
        is_power_of_two = ratio > 0 and (ratio & (ratio - 1)) == 0
        hp_order = ratio.bit_length() - 1 if is_power_of_two else -1
        can_buddy = 0 <= hp_order <= max_order

        avail_all = hugepage_availability_all(buddy, hp_order) if can_buddy else {}
        avail_safe = hugepage_availability_safe(pagetype, hp_order) if (can_buddy and pagetype) else {}

        for node in nodes_present:
            pnode = pool_by_size.get((hp_size, node))
            pool_free = str(pnode["free"]) if pnode else "—"
            pool_total = str(pnode["nr"] + pnode["surplus"]) if pnode else "—"
            if not can_buddy:
                # The buddy allocator can't produce this size directly,
                # but the kernel still attempts compaction + migration
                # when nr_hugepages is increased. Estimate capacity at
                # the pageblock order (typically 2 MiB on x86_64) and
                # divide by blocks_per_hp, since every 1 GiB hugepage
                # needs blocks_per_hp consecutive migratable pageblocks
                # — this tracks fragmentation reality and is also
                # consistent with the smaller-size row (can never
                # exceed the smaller-row count divided by blocks_per_hp).
                blocks_per_hp = hp_size // pageblock_size if pageblock_size else 0
                if pageblock_order <= 0 or blocks_per_hp <= 0:
                    safe_str = "pool only"
                    all_str = "pool only"
                else:
                    pb_all = hugepage_availability_all(buddy, pageblock_order).get(
                        node, {"all": 0}
                    )["all"]
                    all_est = pb_all // blocks_per_hp
                    all_str = (f"≤{all_est:,} (from {pb_all:,} 2M free)"
                                if pb_all else "0 (no 2M free)")
                    if pagetype:
                        pb_safe = hugepage_availability_safe(pagetype, pageblock_order).get(node, 0)
                        safe_est = pb_safe // blocks_per_hp
                        safe_str = (f"≤{safe_est:,} ({pb_safe:,} movable)"
                                    if pb_safe else "0 (no movable)")
                    else:
                        safe_str = "needs root"
            else:
                a = avail_all.get(node, {"all": 0})["all"]
                all_str = f"{a:,} ({human(a * hp_size)})"
                if pagetype:
                    s = avail_safe.get(node, 0)
                    safe_str = f"{s:,} ({human(s * hp_size)})"
                else:
                    safe_str = "needs root"
            vals = (human(hp_size), f"node{node}", pool_free, pool_total, safe_str, all_str)
            print("  " + "  ".join(f"{v:<{w}}" if i < 2 else f"{v:>{w}}"
                                    for i, (v, w) in enumerate(zip(vals, widths))))
    print()


def print_numa(nodes: Dict[int, List[dict]]) -> None:
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


def parse_smaps(pid: str) -> Tuple[Optional[List[dict]], Optional[str]]:
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

    entries: List[dict] = []
    current: Optional[dict] = None
    for line in text.splitlines():
        m = SMAPS_HEADER_RE.match(line)
        if m:
            if current is not None:
                entries.append(current)
            path = (m.group("path") or "").strip()
            perms = m.group("perms")
            try:
                start = int(m.group("start"), 16)
            except (TypeError, ValueError):
                start = 0
            current = {"perms": perms, "path": path, "start": start,
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


def online_numa_nodes() -> List[int]:
    """Return the list of online NUMA node IDs, cached.

    Parses /sys/devices/system/node/online (kernel's cpulist syntax:
    '0-3' or '0,2,4-7'). Falls back to the directory listing if the
    file is missing.
    """
    if _ONLINE_NUMA_NODES_CACHE:
        return _ONLINE_NUMA_NODES_CACHE[0]
    nodes: List[int] = []
    online_file = NUMA_ROOT / "online"
    try:
        text = online_file.read_text().strip()
    except OSError:
        text = ""
    if text:
        for part in text.split(","):
            part = part.strip()
            if "-" in part:
                a, b = part.split("-", 1)
                nodes.extend(range(int(a), int(b) + 1))
            elif part:
                nodes.append(int(part))
    elif NUMA_ROOT.is_dir():
        for entry in NUMA_ROOT.iterdir():
            m = re.match(r"node(\d+)$", entry.name)
            if m:
                nodes.append(int(m.group(1)))
    nodes.sort()
    _ONLINE_NUMA_NODES_CACHE.append(nodes)
    return nodes


def parse_numa_maps(pid: str) -> Optional[Dict[int, dict]]:
    """Parse /proc/<pid>/numa_maps into ``{vma_start: info}``.

    ``info`` is ``{"nodes": {node_id: bytes}, "huge": bool}``. The
    ``huge`` flag is raised by the presence of the ``huge`` token in
    the line, which the kernel emits for hugetlbfs-backed mappings;
    those pages are NOT counted in smaps Rss, so they must be routed
    to the HugeTLB bucket rather than the per-category RSS totals to
    avoid double-counting.

    Each line carries its own ``kernelpagesize_kB`` — 4 for regular
    pages, 2048 for THP, 1048576 for 1 GiB hugetlb — so every line is
    scaled individually before summing.
    """
    try:
        text = (PROC / pid / "numa_maps").read_text()
    except (PermissionError, FileNotFoundError):
        return None
    except OSError:
        return None
    result: Dict[int, dict] = {}
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        try:
            addr = int(parts[0], 16)
        except ValueError:
            continue
        page_kb = 4
        raw: List[Tuple[int, int]] = []
        is_huge = False
        for tok in parts[1:]:
            if tok == "huge":
                is_huge = True
            elif tok.startswith("kernelpagesize_kB="):
                try:
                    page_kb = int(tok.split("=", 1)[1])
                except ValueError:
                    pass
            elif tok and tok[0] == "N" and "=" in tok:
                key, _, val = tok.partition("=")
                try:
                    raw.append((int(key[1:]), int(val)))
                except ValueError:
                    pass
        if not raw:
            continue
        scale = page_kb * 1024
        per_node: Dict[int, int] = {}
        for node, count in raw:
            per_node[node] = per_node.get(node, 0) + count * scale
        result[addr] = {"nodes": per_node, "huge": is_huge}
    return result


def compact_size(nbytes: int) -> str:
    """Short byte formatting (``2.1G``, ``512M``, ``48K``, ``0``) for
    dense per-node columns where a full ``human()`` would be too wide."""
    if nbytes <= 0:
        return "0"
    if nbytes >= 1 << 30:
        return f"{nbytes / (1 << 30):.1f}G"
    if nbytes >= 1 << 20:
        return f"{nbytes / (1 << 20):.0f}M"
    if nbytes >= 1 << 10:
        return f"{nbytes / (1 << 10):.0f}K"
    return str(nbytes)


def format_numa_rss(per_node: Dict[int, int], nodes: List[int]) -> str:
    """``N0/N1/N2/...`` compact per-node RSS string."""
    return "/".join(compact_size(per_node.get(n, 0)) for n in nodes)


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
CGROUP_PATTERNS: List[Tuple[Pattern[str], str]] = [
    (re.compile(r"/kubepods[.\-/].*?(?:pod)?([0-9a-f]{8}[-_][0-9a-f]{4,}[^/]*)"), "k8s"),
    (re.compile(r"/kubepods[.\-/]"), "k8s"),
    (re.compile(r"docker[-/]([0-9a-f]{12,64})"), "docker"),
    (re.compile(r"libpod[-/]([0-9a-f]{12,64})"), "podman"),
    (re.compile(r"crio[-/]([0-9a-f]{12,64})"), "crio"),
    (re.compile(r"containerd[-/]([0-9a-f]{12,64})"), "containerd"),
    (re.compile(r"/lxc(?:\.payload)?[./]([^/]+)"), "lxc"),
    (re.compile(r"machine-([^./]+)\.scope"), "nspawn"),
]

_HOST_NS_CACHE: List[Optional[int]] = []


def pid_namespace_inode(pid: str) -> Optional[int]:
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


def host_pid_namespace() -> Optional[int]:
    """The PID namespace inode of the memsleuth process itself.

    Cached because it's invariant for the run. We use /proc/self because
    /proc/1 is often unreadable for non-root users, but /proc/self is
    always readable by us.
    """
    if not _HOST_NS_CACHE:
        _HOST_NS_CACHE.append(pid_namespace_inode("self"))
    return _HOST_NS_CACHE[0]


def read_cgroup_info(pid: str) -> Tuple[List[str], Optional[str]]:
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
    all_paths: List[str] = []
    unified: Optional[str] = None
    v1_fallback: Optional[str] = None
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


def container_label_from_cgroup(path: Optional[str]) -> Optional[str]:
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


def classify_container(pid: str) -> Tuple[str, str]:
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

# Categories carried over to NUMA sub-rows. Code/heap/stack/anon/data
# map one VMA to one bucket directly. shmem maps to data_rss too (it
# typically means /dev/shm or MAP_SHARED+MAP_ANON — both "data" for
# layout purposes). file-data (non-exec file mappings) deliberately
# does NOT contribute to AnonData.
NUMA_CAT_MAP = {
    "code": "code_rss",
    "heap": "heap_rss",
    "stack": "stack_rss",
    "anon": "data_rss",
    "shmem": "data_rss",
}
NUMA_FIELDS = ("rss", "code_rss", "heap_rss", "stack_rss",
               "data_rss", "shared_rss", "hugetlb")


def aggregate_process(entries: List[dict], keep_segments: bool = False,
                      numa_data: Optional[Dict[int, dict]] = None) -> dict:
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
    segments: List[dict] = [] if keep_segments else []
    numa: Dict[str, Dict[int, int]] = {f: {} for f in NUMA_FIELDS} if numa_data is not None else {}

    def _add(bucket: str, node: int, amount: int) -> None:
        d = numa[bucket]
        d[node] = d.get(node, 0) + amount

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

        if numa_data is not None:
            info = numa_data.get(v["start"])
            if info:
                nodes = info["nodes"]
                if info["huge"]:
                    # Hugetlbfs pages: excluded from smaps Rss, accounted
                    # only on the HugeTLB track so sums line up.
                    for n, b in nodes.items():
                        _add("hugetlb", n, b)
                else:
                    for n, b in nodes.items():
                        _add("rss", n, b)
                    cat_key = NUMA_CAT_MAP.get(cat)
                    if cat_key:
                        for n, b in nodes.items():
                            _add(cat_key, n, b)
                    # Shared is a property of pages (not the VMA), so
                    # attribute proportionally by Shared / Rss.
                    if v["rss"] > 0 and shared > 0:
                        frac = shared / v["rss"]
                        for n, b in nodes.items():
                            _add("shared_rss", n, int(b * frac))

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
        merged: Dict[Tuple[str, str], dict] = {}
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
    if numa_data is not None:
        for f in NUMA_FIELDS:
            agg[f"numa_{f}"] = numa[f]
    return agg


def collect_process_details(keep_segments: bool = False,
                              include_numa: bool = False) -> Tuple[List[dict], int, int]:
    """Walk /proc scanning smaps. Returns (rows, denied, kernel_threads).

    When ``include_numa`` is True, each row also carries a
    ``numa_rss`` dict {node_id: bytes} parsed from /proc/<pid>/numa_maps.
    """
    rows: List[dict] = []
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
        numa_data = parse_numa_maps(entry.name) if include_numa else None
        agg = aggregate_process(entries, keep_segments=keep_segments,
                                  numa_data=numa_data)
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


def _print_process_table(rows: List[dict], top: Optional[int],
                          show_segments: bool, indent: str,
                          numa_nodes: Optional[List[int]] = None) -> None:
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
        if numa_nodes:
            _print_numa_subrows(r, numa_nodes, indent)
        if show_segments:
            _print_segments(r.get("segments", []))
    if top and len(rows) > top:
        print(f"{indent}... {len(rows) - top} more (use --top 0 for all)")


def _print_numa_subrows(row: dict, nodes: List[int], indent: str) -> None:
    """Emit one ``N<id>`` sub-row per NUMA node with per-category bytes.

    The node label sits in the Command column so each sub-row reads as
    an annotation of the process above it. Swap / ExeSwap / FileSwap
    show as ``—`` (disk-backed, not NUMA-resident); THP columns show
    as ``—`` too — they're a subset of RSS and already broken out by
    node via the RSS/Code/AnonData cells.
    """
    na = "—"
    rss = row.get("numa_rss", {})
    code = row.get("numa_code_rss", {})
    heap = row.get("numa_heap_rss", {})
    stack = row.get("numa_stack_rss", {})
    anon = row.get("numa_data_rss", {})
    shared = row.get("numa_shared_rss", {})
    hugetlb = row.get("numa_hugetlb", {})
    for n in nodes:
        vals = (
            "",
            f"N{n}",
            compact_size(rss.get(n, 0)),
            compact_size(code.get(n, 0)),
            compact_size(heap.get(n, 0)),
            compact_size(stack.get(n, 0)),
            compact_size(anon.get(n, 0)),
            compact_size(shared.get(n, 0)),
            na, na, na,          # Swap / ExeSwap / FileSwap
            na, na,              # THP/code / THP/data
            compact_size(hugetlb.get(n, 0)),
        )
        print(indent + "  ".join(f"{v:<{w}}" if i < 2 else f"{v:>{w}}"
                                  for i, (v, w) in enumerate(
                                      zip(vals, PROC_TABLE_WIDTHS))))


def aggregate_containers(rows: List[dict]) -> List[dict]:
    """Sum per-process stats into per-container totals."""
    fields = ("rss", "code_rss", "heap_rss", "stack_rss", "data_rss",
              "shared_rss", "swap", "exe_ondisk", "file_ondisk",
              "thp_code", "thp_data", "hugetlb_priv", "hugetlb_shared")
    bucket: Dict[str, dict] = {}
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


def _print_container_summary(containers: List[dict]) -> None:
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


def print_process_details(top: Optional[int], show_segments: bool,
                           group_by_container: bool,
                           show_numa: bool = False) -> None:
    numa_nodes = online_numa_nodes() if show_numa else []
    # Only worth adding the column when the host is actually multi-node.
    numa_nodes = numa_nodes if len(numa_nodes) > 1 else []
    rows, denied, gone = collect_process_details(
        keep_segments=show_segments,
        include_numa=bool(numa_nodes),
    )
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
            _print_process_table(proc_rows, top, show_segments, indent="    ",
                                  numa_nodes=numa_nodes or None)
    else:
        print("Per-process memory detail")
        if not rows:
            print("  (no processes with user memory found)")
        else:
            _print_process_table(rows, top, show_segments, indent="  ",
                                  numa_nodes=numa_nodes or None)

    if has_containers:
        _print_container_summary(containers)

    _print_hugetlb_table(rows, top)
    _print_proc_notes(denied, gone)


def _print_segments(segments: List[dict]) -> None:
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


def _print_hugetlb_table(rows: List[dict], top: Optional[int]) -> None:
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


def print_thp(mem: Dict[str, int]) -> None:
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


def print_directmap(mem: Dict[str, int]) -> None:
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

Hugepage allocation capacity (always shown when any pool size is configured)
----------------------------------------------------------------------------
Per-NUMA answer to "could I allocate a hugepage right now, and how many?"

  Pool free / Pool total  /sys/kernel/mm/hugepages-*kB/{free,nr}_hugepages per node
                          (plus surplus). Already reserved, immediately usable.
  Buddy safe              Free blocks of order ≥ the hugepage order, in
                          Movable / Reclaimable / CMA migration pools. These
                          are allocatable without migrating in-use kernel
                          data. Pulled from /proc/pagetypeinfo — root-only;
                          renders as 'needs root' otherwise.
  Buddy max               Free blocks at the right order summed across ALL
                          migration types (Unmovable included). The extra
                          pages beyond 'Buddy safe' may require compaction/
                          migration and are not guaranteed to succeed. From
                          /proc/buddyinfo (world-readable).

Larger-order free blocks contribute multiple hugepages — an order-K block
satisfies ``2**(K - hp_order)`` hugepages of the target size, since the
kernel can split it.

Sizes beyond MAX_ORDER × base-page-size (typically 1 GiB on x86_64, where
MAX_ORDER caps the buddy allocator around 4 MiB) can't come directly
from the buddy allocator, but the kernel will still try compaction +
migration when you bump ``nr_hugepages``. For those rows the columns
are computed at the pageblock order (the smallest hugepage size, 2
MiB on x86_64) and divided by the ratio hp_size / pageblock_size:

  Buddy safe  ≤N (M movable)    - N is the optimistic count assuming
                                  contiguity: M free 2 MiB blocks in
                                  Movable/Reclaimable/CMA pools,
                                  divided by blocks-per-hugepage.
                                  Needs /proc/pagetypeinfo (root).
  Buddy max   ≤N (from M 2M free)
                                - N is M / blocks-per-hugepage, M = all
                                  free 2 MiB blocks at the pageblock
                                  order (from /proc/buddyinfo, world-
                                  readable). Consistent with the
                                  smaller-size row: you can't get more
                                  1 GiB pages than the 2 MiB count
                                  divided by 512.

Both are optimistic — the 2 MiB blocks aren't guaranteed to be
contiguous at 1 GiB boundaries — but they're tighter and more honest
than a MemFree-based ceiling. To add new 1 GiB pages reliably, prefer
boot-time ``hugepagesz=1G hugepages=N`` on the kernel command line or
a ``hugetlb_cma=`` reservation.

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

Per-process NUMA sub-rows (--numa with --procs)
-----------------------------------------------
On multi-node hosts, each process row is followed by one ``N<id>`` sub-row per online NUMA node.
Each sub-row breaks down where that process's pages live by category, using compact sizes
('2.1G', '512M', '48K', '0').

Sub-row columns:
  RSS        Non-hugetlb resident bytes on this node (regular 4 KiB + 2 MiB THP).
  Code       File-backed executable bytes on this node.
  Heap       [heap] bytes on this node.
  Stack      [stack] / [stack:tid] bytes on this node.
  AnonData   Anonymous mapping bytes on this node (shmem merged in).
  Shared     Shared_Clean + Shared_Dirty share of the node's bytes, attributed proportionally by
             (Shared / Rss) per VMA — not a direct kernel count, so the per-node sum may be a few
             bytes off the total Shared column due to rounding.
  HugeTLB    Hugetlbfs pages on this node (kernel-reported; these are NOT counted in RSS so the
             HugeTLB column and the RSS sub-row numbers don't overlap).
  Swap / ExeSwap / FileSwap / THP/code / THP/data  render as '—' in sub-rows — swap is disk-backed
  (no NUMA attribution), and THP columns are already accounted inside the RSS/Code/AnonData cells.

Data source is /proc/<pid>/numa_maps. Each VMA line reports ``N<id>=<pages>`` and
``kernelpagesize_kB`` — the parser matches each line to its smaps VMA by start address and scales
by the line's own page size (4 KiB, 2 MiB for THP, 1 GiB for hugetlb). The ``huge`` token in a
numa_maps line routes those bytes to HugeTLB instead of RSS, so the two sets don't double-count.

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


def main(argv: Optional[List[str]] = None) -> int:
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
                    help="break down hugepages per NUMA node; with --procs also adds a per-process "
                         "'RSS by NUMA' column showing per-node residency (N0/N1/... compact)")
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
    pools = collect_hugepages()
    numa_hugetlb = collect_numa_hugepages()
    print_free(mem)
    print_hugetlb(pools)
    if args.numa:
        print_numa(numa_hugetlb)
    print_hugepage_capacity(pools, numa_hugetlb)
    if not args.no_thp:
        print_thp(mem)
    if args.procs or args.shared or args.containers:
        print_process_details(args.top or None,
                               show_segments=args.shared,
                               group_by_container=args.containers,
                               show_numa=args.numa)
    if not args.no_directmap:
        print_directmap(mem)
    return 0


if __name__ == "__main__":
    sys.exit(main())
