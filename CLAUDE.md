# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running

`memsleuth` is a single-file Python 3 script, stdlib-only (no deps, no build, no tests yet).

```bash
./memsleuth.py                  # free-style summary + hugetlb pools + THP + DirectMap
./memsleuth.py --procs          # add per-process memory breakdown
./memsleuth.py --shared         # also list top shared VMAs under each process
./memsleuth.py --numa           # split hugepage pools by NUMA node
./memsleuth.py --containers     # group per-process listing by container runtime
./memsleuth.py --help-fields    # long-form reference for every output column
sudo ./memsleuth.py --procs     # needed to attribute memory for other users' processes
```

Per-process mode requires `root` or `CAP_SYS_PTRACE` to read `/proc/<pid>/smaps` for processes you don't own; kernel threads are distinct from permission-denied processes and the code reports them separately on purpose.

## Architecture

All logic lives in `memsleuth.py`. The flow is **collect → aggregate → print**, one pipeline per section:

| Section              | Data source                                                     | Collector                                  | Printer                |
|----------------------|-----------------------------------------------------------------|--------------------------------------------|------------------------|
| free-style summary   | `/proc/meminfo`                                                 | `parse_meminfo`                            | `print_free`           |
| Hugetlb pools        | `/sys/kernel/mm/hugepages/hugepages-*kB/`                       | `collect_hugepages`                        | `print_hugetlb`        |
| NUMA hugetlb         | `/sys/devices/system/node/node*/hugepages/`                     | `collect_numa_hugepages`                   | `print_numa`           |
| THP / DirectMap      | `/proc/meminfo` (same dict)                                     | —                                          | `print_thp` / `print_directmap` |
| Per-process          | `/proc/<pid>/{smaps,cmdline,comm}`                              | `parse_smaps` → `aggregate_process` → `collect_process_details` | `print_process_details` |

### Per-process pipeline (the non-trivial part)

`parse_smaps(pid)` returns `(entries, err)` where `err` is `"gone"` (ENOENT — kernel thread or exited process) or `"denied"` (EACCES — real permission problem). Keep those distinct; lumping them was a real bug. Each VMA entry carries the fields in `SMAPS_FIELDS` plus a `category` from `categorize_vma(path, perms)`:

- `code` — file-backed VMA with `x` in perms (main binary + `.so` libraries)
- `file-data` — file-backed VMA without `x` (rodata, mmap'd data)
- `heap`, `stack`, `vdso`, `hugetlb`, `other` — recognized from path
- `anon` / `shmem` — anonymous (no path); `shmem` when perms contain `s`

`aggregate_process(entries, keep_segments=False)` rolls VMAs up into a per-process summary. Key derived fields:

- `thp_code`, `thp_data`: AnonHugePages/FilePmdMapped/ShmemPmdMapped split by whether the VMA is executable (this is how the tool answers "is THP backing code or data?").
- `swap`: sum of smaps `Swap` across all VMAs — includes COW'd pages from private file mappings (they become anon after a write).
- `exe_ondisk` / `file_ondisk`: `max(0, Size - Rss)` for `code` / `file-data` VMAs. These conflate "evicted" with "never faulted"; that caveat is documented in `--help-fields` and should be preserved in any future changes.
- `shared_rss`: `Shared_Clean + Shared_Dirty` summed.
- When `keep_segments=True`, segments with `≥ SEGMENT_MIN_SHARED` shared bytes are kept, merged by `(path, perms)` so one file's `r-xp` / `r--p` / `rw-p` ranges show as separate logical rows.

`~sharers` in the shared-segment output is approximated as `round(Rss / Pss)` per VMA. The kernel doesn't directly expose how many processes map a region; `Pss` gives us that for free.

### Per-process NUMA attribution

`--numa` with `--procs` emits one `N<id>` sub-row per online NUMA node under each process, covering RSS / Code / Heap / Stack / AnonData / Shared / HugeTLB. Two data sources are cross-referenced:

- `/proc/<pid>/smaps` gives the VMA category (`code`, `heap`, etc.) and start address.
- `/proc/<pid>/numa_maps` gives per-VMA `N<id>=<pages>`, `kernelpagesize_kB`, and a `huge` token for hugetlbfs mappings.

`parse_numa_maps` returns `{vma_start: {"nodes": {n: bytes}, "huge": bool}}`. `aggregate_process` matches by `start` — this is why `SMAPS_HEADER_RE` captures `(?P<start>[0-9a-f]+)`. The `huge` flag is critical: hugetlbfs pages are NOT in smaps Rss (they live under `Private_Hugetlb` / `Shared_Hugetlb`), so those bytes route to the `hugetlb` bucket and never touch RSS. Without that split the per-NUMA sum would double-count hugetlb workloads like Weka.

`Shared` per-node is attributed proportionally (`Shared / Rss` per VMA); the kernel doesn't expose a direct per-page shared count. Every other category's VMAs map one-to-one to a bucket. `online_numa_nodes()` reads `/sys/devices/system/node/online` (kernel cpulist syntax, cached). Sub-rows are suppressed on single-node hosts. `compact_size()` (`2.1G`, `512M`, `48K`, `0`) replaces `human()` in sub-rows so all cells stay within the main table's column widths. Swap/ExeSwap/FileSwap/THP cells render as `—` because they're disk-backed or already accounted inside the per-node RSS.

### Container classification

`classify_container(pid)` reads `/proc/<pid>/cgroup` via `read_cgroup_info`, which returns **every** hierarchy's path plus a "primary" (v2 unified, else v1 memory/pids). Container detection scans all paths — this matters for runtimes that pin the container id on a **named v1 hierarchy** (e.g. Weka writes `name=weka:/container/weka/default3` while the memory/pids/unified controllers all point at `/system.slice/weka-agent.service`). Looking only at the unified line silently misidentifies every such process as `system.slice`. The structural `system.slice` / `user.slice` / `system` buckets (steps 3–5 below) use only the primary path. Priority:

1. `/container/<runtime>/<id>[/...]` → `<runtime>:<id>` — catches Weka's custom layout (`/container/weka/default0` etc.); deeper sub-cgroups inside a container collapse into the same bucket via `CONTAINER_SLOT_RE` capturing the first two segments.
2. `CGROUP_PATTERNS` against the cgroup path: docker, podman/libpod, kubepods, crio, containerd, lxc, systemd-nspawn (`machine-*.scope`). Labelled `<runtime>:<id>`.
3. `/system.slice/*` → one `system.slice` bucket (all systemd services grouped).
4. `/user.slice/*` → one `user.slice` bucket (all user sessions grouped — we intentionally do not split by UID because the typical ask is "host vs. containers", not per-user rollups).
5. Everything else (`/`, `/init.scope`, unreadable cgroup) → `system`.

`pid_namespace_inode` and `host_pid_namespace` are present but deliberately not the primary signal: browser sandboxes each get their own PID namespace and treating every sandbox as a container drowns the real ones in noise. The helpers remain for a potential future flag that surfaces raw namespace splits.

Summary gate (`has_containers`) triggers when classification produced more than one bucket, or when `--containers` was explicitly requested. All per-container numbers are summed from our own smaps data — the tool never reads cgroup memory accounting, so numbers are consistent with the per-process view and don't depend on in-container instrumentation. Bucket ordering in the summary places real containers first (by RSS desc) and pushes `system.slice` / `user.slice` / `system` to the end in that fixed order, keeping the interesting rows at the top.

### Formatting conventions

- `human(nbytes)` is the single source of truth for byte formatting (PiB/TiB/GiB/MiB/KiB).
- `truncate(s, width)` uses `…` (single char) as the overflow marker so column alignment stays correct.
- `read_process_name(pid)` joins `/proc/<pid>/cmdline` on NUL and reduces `argv[0]` to basename so `/usr/lib/firefox/firefox-bin -contentproc` renders as `firefox-bin -contentproc`.
- Per-process tables are laid out for a ~120-column terminal minimum; the argparse help formatter is pinned to `width=120` to match.

### Invariant

`FIELDS_HELP` (printed by `--help-fields`) documents every column in every table. When columns or semantics change, update `FIELDS_HELP` in the same edit — the epilog of `--help` points users at it.
