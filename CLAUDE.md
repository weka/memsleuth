# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running

`memsleuth` is a single-file Python 3 script, stdlib-only (no deps, no build, no tests yet).

```bash
./memsleuth.py                  # free-style summary + hugetlb pools + THP + DirectMap
./memsleuth.py --procs          # add per-process memory breakdown
./memsleuth.py --shared         # also list top shared VMAs under each process
./memsleuth.py --numa           # split hugepage pools by NUMA node
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

### Formatting conventions

- `human(nbytes)` is the single source of truth for byte formatting (PiB/TiB/GiB/MiB/KiB).
- `truncate(s, width)` uses `…` (single char) as the overflow marker so column alignment stays correct.
- `read_process_name(pid)` joins `/proc/<pid>/cmdline` on NUL and reduces `argv[0]` to basename so `/usr/lib/firefox/firefox-bin -contentproc` renders as `firefox-bin -contentproc`.
- Per-process tables are laid out for a ~120-column terminal minimum; the argparse help formatter is pinned to `width=120` to match.

### Invariant

`FIELDS_HELP` (printed by `--help-fields`) documents every column in every table. When columns or semantics change, update `FIELDS_HELP` in the same edit — the epilog of `--help` points users at it.
