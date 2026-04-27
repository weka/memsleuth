# memsleuth

A `free`-on-steroids for Linux: overall memory, a full breakdown of every configured hugepage size, per-process memory attribution (RSS / code / heap / stack / anon), swap usage split by destination, shared-segment attribution with sharer counts, and container-level rollups. Single Python 3 file, stdlib only, no install step.

`memsleuth` was built to answer questions the usual tools can't:

- Where did all the 1 GiB hugepages go? Which pool? Who's using them?
- Is THP backing my code or just data?
- What's actually swapped to the swap file vs. "just" paged out to the binary?
- Which processes share that huge libxul, and how many other processes pin the same page?
- On a host running LXC / docker / k8s containers: how does memory break down per container, without trusting the container's own accounting?

## Requirements

- Linux with `/proc` and `/sys/kernel/mm/hugepages`.
- Python 3.6+ (stdlib only; annotations use the `typing` module to work on older interpreters).
- Per-process modes (`--procs`, `--shared`, `--containers`) require permission to read `/proc/<pid>/smaps` for the targets. Run as root or with `CAP_SYS_PTRACE` to attribute memory across all users.

## Install

```bash
git clone <this repo>
./memsleuth/memsleuth.py
```

That's it — no pip, no build.

## Quick start

```bash
./memsleuth.py                    # free-style + hugetlb pools + THP + DirectMap
./memsleuth.py --procs            # add per-process memory breakdown
./memsleuth.py --shared           # also list each process's top shared segments
./memsleuth.py --numa             # split hugepage pools by NUMA node
./memsleuth.py --containers       # group the per-process listing by container
sudo ./memsleuth.py --procs       # needed to attribute memory for other users' processes
sudo ./memsleuth.py --unlink              # remove unused hugetlbfs files
sudo ./memsleuth.py --release             # set nr_hugepages=0 for every size
sudo ./memsleuth.py --unlink --release    # unlink first, then release; reclaims most
./memsleuth.py --doctor                   # only fix recommendations; quiet when clean
./memsleuth.py --help             # flag reference
./memsleuth.py --help-fields      # detailed explanation of every column
```

`--top N` caps the per-process (or per-container) listing; `--top 0` shows everything.

## Sections

### 1. `free`-style summary + hugepage pools

Default output: a `free(1)`-style line for RAM and swap, then the full hugepage pool table — **one row per configured size** (2 MiB, 1 GiB, ...), with Total / Free / Reserved / Surplus / Overcommit counts plus "Mem Used" and "Mem Free" in bytes. Transparent huge pages (AnonHugePages, ShmemHugePages, FileHugePages) and the kernel direct map split (`DirectMap4k/2M/1G`) follow underneath.

```text
Memory
               total        used        free      shared    buff/cache     available
Mem:      503.45 GiB  140.91 GiB  315.34 GiB    2.38 GiB     47.20 GiB    359.26 GiB
Swap:       8.00 GiB         0 B    8.00 GiB

HugeTLB Pages (explicit hugepage pools)
         Size    Total     Free     Rsvd  Surplus  Overcmt     Used     Mem Used     Mem Free
     2.00 MiB     7260        0        0        0        0     7260    14.18 GiB          0 B
     1.00 GiB       65        0        0        0        0       65    65.00 GiB          0 B
  Pool total: 79.18 GiB  used: 79.18 GiB  free: 0 B

Kernel Direct Map (physical memory mapped by page size)
  DirectMap4k:        9.26 GiB
  DirectMap2M:      116.38 GiB
  DirectMap1G:      388.00 GiB
```

### 2. Per-process detail (`--procs`)

Full breakdown for every process the current user can read (root sees everything). Columns: PID, Command (full `cmdline`, argv[0] basenamed, truncated at 44 chars with `…`), RSS, Code, Heap, Stack, AnonData, Shared, Swap, ExeSwap, FileSwap, THP/code, THP/data, HugeTLB.

The three swap-ish columns distinguish where the evicted memory lives:

- **Swap**: in the swap device (anon pages, including COW'd pages from private file mappings).
- **ExeSwap**: executable file pages not in RAM — would re-read from the binary / `.so`.
- **FileSwap**: non-exec file pages not in RAM — fonts, `locale-archive`, mmap'd data files, ...

```text
Memory
               total        used        free      shared    buff/cache     available
Mem:      503.45 GiB  140.88 GiB  315.37 GiB    2.38 GiB     47.20 GiB    359.29 GiB
Swap:       8.00 GiB         0 B    8.00 GiB

HugeTLB Pages (explicit hugepage pools)
         Size    Total     Free     Rsvd  Surplus  Overcmt     Used     Mem Used     Mem Free
     2.00 MiB     7260        0        0        0        0     7260    14.18 GiB          0 B
     1.00 GiB       65        0        0        0        0       65    65.00 GiB          0 B
  Pool total: 79.18 GiB  used: 79.18 GiB  free: 0 B

Per-process memory detail
  PID       Command                                                RSS          Code          Heap         Stack      AnonData        Shared          Swap       ExeSwap      FileSwap      THP/code      THP/data       HugeTLB
  845836    wekanode --slot 0 --container-name default4       3.55 GiB    104.50 MiB      7.08 MiB    132.00 KiB      2.22 GiB    291.79 MiB           0 B           0 B           0 B           0 B           0 B           0 B
  845837    wekanode --slot 0 --container-name default3       3.53 GiB    104.50 MiB      6.65 MiB    132.00 KiB      2.21 GiB    297.75 MiB           0 B           0 B           0 B           0 B           0 B           0 B
  845825    wekanode --slot 0 --container-name default1       3.46 GiB    104.50 MiB      4.58 MiB    132.00 KiB      2.14 GiB    287.75 MiB           0 B           0 B           0 B           0 B           0 B           0 B
  845830    wekanode --slot 0 --container-name default2       3.46 GiB    104.50 MiB      4.10 MiB    132.00 KiB      2.14 GiB    286.75 MiB           0 B           0 B           0 B           0 B           0 B           0 B
  845845    wekanode --slot 0 --container-name default0       3.46 GiB    104.50 MiB      4.57 MiB    132.00 KiB      2.14 GiB    298.75 MiB           0 B           0 B           0 B           0 B           0 B           0 B
  847602    wekanode --slot 2 --container-name default3       3.40 GiB    117.28 MiB     50.80 MiB    132.00 KiB      2.90 GiB    319.50 MiB           0 B           0 B     13.01 GiB           0 B           0 B     13.01 GiB
  848861    wekanode --slot 2 --container-name default2       3.40 GiB    117.28 MiB     42.19 MiB    132.00 KiB      2.90 GiB    319.50 MiB           0 B           0 B     13.01 GiB           0 B           0 B     13.01 GiB
  847090    wekanode --slot 2 --container-name default4       3.40 GiB    117.28 MiB     41.52 MiB    132.00 KiB      2.90 GiB    319.50 MiB           0 B           0 B     13.01 GiB           0 B           0 B     13.01 GiB
  848842    wekanode --slot 2 --container-name default0       3.40 GiB    117.28 MiB     40.86 MiB    132.00 KiB      2.90 GiB    319.50 MiB           0 B           0 B     13.01 GiB           0 B           0 B     13.01 GiB
  848838    wekanode --slot 2 --container-name default1       3.37 GiB    117.28 MiB     44.64 MiB    132.00 KiB      2.88 GiB    319.50 MiB           0 B           0 B     13.01 GiB           0 B           0 B     13.01 GiB
  847066    wekanode --slot 1 --container-name default4       2.18 GiB    117.28 MiB     21.00 MiB    132.00 KiB      1.54 GiB    293.52 MiB           0 B           0 B      1.43 GiB           0 B           0 B      1.38 GiB
  847598    wekanode --slot 1 --container-name default3       2.18 GiB    117.28 MiB     21.00 MiB    132.00 KiB      1.54 GiB    297.50 MiB           0 B           0 B      1.43 GiB           0 B           0 B      1.38 GiB
  848839    wekanode --slot 1 --container-name default0       2.18 GiB    117.28 MiB     21.00 MiB    132.00 KiB      1.54 GiB    297.50 MiB           0 B           0 B      1.43 GiB           0 B           0 B      1.38 GiB
  848845    wekanode --slot 1 --container-name default1       2.18 GiB    117.28 MiB     21.00 MiB    132.00 KiB      1.54 GiB    294.54 MiB           0 B           0 B      1.43 GiB           0 B           0 B      1.38 GiB
  848846    wekanode --slot 1 --container-name default2       2.18 GiB    117.28 MiB     21.00 MiB    132.00 KiB      1.54 GiB    293.56 MiB           0 B           0 B      1.43 GiB           0 B           0 B      1.38 GiB
  ... 136 more (use --top 0 for all)

Per-container summary (grouped by PID namespace / cgroup)
  Container                      Procs           RSS          Code          Heap         Stack      AnonData        Shared          Swap       ExeSwap      FileSwap      THP/code      THP/data       HugeTLB
  weka:default3                     22     11.78 GiB    568.57 MiB    153.23 MiB      2.22 MiB      8.53 GiB      1.44 GiB           0 B     56.13 MiB     16.38 GiB           0 B           0 B     15.84 GiB
  weka:default4                     22     11.76 GiB    568.17 MiB    143.91 MiB      2.23 MiB      8.53 GiB      1.41 GiB           0 B     56.53 MiB     16.39 GiB           0 B           0 B     15.84 GiB
  weka:default0                     22     11.69 GiB    567.73 MiB    141.34 MiB      2.21 MiB      8.45 GiB      1.44 GiB           0 B     56.96 MiB     16.38 GiB           0 B           0 B     15.84 GiB
  weka:default2                     22     11.65 GiB    567.98 MiB    141.55 MiB      2.21 MiB      8.43 GiB      1.39 GiB           0 B     56.71 MiB     16.40 GiB           0 B           0 B     15.84 GiB
  weka:default1                     22     11.64 GiB    567.45 MiB    144.41 MiB      2.23 MiB      8.41 GiB      1.40 GiB           0 B     57.25 MiB     16.40 GiB           0 B           0 B     15.84 GiB
  system.slice                      24    538.12 MiB    135.05 MiB     28.07 MiB    888.00 KiB    159.80 MiB    215.40 MiB           0 B    108.44 MiB    179.80 MiB           0 B           0 B           0 B
  user.slice                        16    172.63 MiB     62.82 MiB     45.68 MiB      2.33 MiB     24.12 MiB     91.49 MiB           0 B     32.63 MiB     46.46 MiB           0 B           0 B           0 B
  system                             1     14.00 MiB      6.05 MiB      3.29 MiB     52.00 KiB    176.00 KiB      9.36 MiB           0 B      5.90 MiB      2.18 MiB           0 B           0 B           0 B

HugeTLB (hugetlbfs) users — Private / Shared
  PID       Command                                              Private          Shared           Total
  847602    wekanode --slot 2 --container-name default3        13.01 GiB             0 B       13.01 GiB
  848861    wekanode --slot 2 --container-name default2        13.01 GiB             0 B       13.01 GiB
  847090    wekanode --slot 2 --container-name default4        13.01 GiB             0 B       13.01 GiB
  848842    wekanode --slot 2 --container-name default0        13.01 GiB             0 B       13.01 GiB
  848838    wekanode --slot 2 --container-name default1        13.01 GiB             0 B       13.01 GiB
  847023    wekanode --slot 3 --container-name default4         1.45 GiB             0 B        1.45 GiB
  847593    wekanode --slot 3 --container-name default3         1.45 GiB             0 B        1.45 GiB
  848840    wekanode --slot 3 --container-name default1         1.45 GiB             0 B        1.45 GiB
  848852    wekanode --slot 3 --container-name default2         1.45 GiB             0 B        1.45 GiB
  848879    wekanode --slot 3 --container-name default0         1.45 GiB             0 B        1.45 GiB
  847066    wekanode --slot 1 --container-name default4         1.38 GiB             0 B        1.38 GiB
  847598    wekanode --slot 1 --container-name default3         1.38 GiB             0 B        1.38 GiB
  848839    wekanode --slot 1 --container-name default0         1.38 GiB             0 B        1.38 GiB
  848845    wekanode --slot 1 --container-name default1         1.38 GiB             0 B        1.38 GiB
  848846    wekanode --slot 1 --container-name default2         1.38 GiB             0 B        1.38 GiB

  note: 4 kernel threads / exited processes skipped

Kernel Direct Map (physical memory mapped by page size)
  DirectMap4k:        9.26 GiB
  DirectMap2M:      116.38 GiB
  DirectMap1G:      388.00 GiB
```

### 3. Shared segments (`--shared`)

Under each process, `--shared` lists the top VMAs the process shares with other processes, merged by `(path, perms)`. The `~sharers` column is `round(Rss / Pss)` — an approximation of how many processes map the same region.

```text
Memory
               total        used        free      shared    buff/cache     available
Mem:      503.45 GiB  140.92 GiB  315.32 GiB    2.38 GiB     47.21 GiB    359.26 GiB
Swap:       8.00 GiB         0 B    8.00 GiB

HugeTLB Pages (explicit hugepage pools)
         Size    Total     Free     Rsvd  Surplus  Overcmt     Used     Mem Used     Mem Free
     2.00 MiB     7260        0        0        0        0     7260    14.18 GiB          0 B
     1.00 GiB       65        0        0        0        0       65    65.00 GiB          0 B
  Pool total: 79.18 GiB  used: 79.18 GiB  free: 0 B

Per-process memory detail
  PID       Command                                                RSS          Code          Heap         Stack      AnonData        Shared          Swap       ExeSwap      FileSwap      THP/code      THP/data       HugeTLB
  845836    wekanode --slot 0 --container-name default4       3.55 GiB    104.50 MiB      7.08 MiB    132.00 KiB      2.22 GiB    292.79 MiB           0 B           0 B           0 B           0 B           0 B           0 B
              RSS      Shared  ~sharers  perms  path
       161.32 MiB  161.29 MiB      ~20x   r--p  ...data/agent/default4_4.4.22.114-32b9a7c0147a0274963a1f587fe796bc/weka/wekanode
       101.98 MiB  101.98 MiB      ~20x   r-xp  ...data/agent/default4_4.4.22.114-32b9a7c0147a0274963a1f587fe796bc/weka/wekanode
        32.52 MiB   20.62 MiB       ~1x   rw-s  ...b9a7c0147a0274963a1f587fe796bc/opt/weka/shm/wekanode_slot0_main.v3.trace_area
        32.52 MiB    2.19 MiB       ~1x   rw-s  ...a7c0147a0274963a1f587fe796bc/opt/weka/shm/wekanode_slot0_shared.v3.trace_area
         1.53 MiB    1.53 MiB      ~92x   r-xp  ....4.22.114-32b9a7c0147a0274963a1f587fe796bc/usr/lib/x86_64-linux-gnu/libc.so.6
         1.20 MiB    1.20 MiB       ~2x   rw-s  ...fault4_4.4.22.114-32b9a7c0147a0274963a1f587fe796bc/opt/weka/shm/events/80.shm
       612.00 KiB  612.00 KiB       ~2x   rw-s  ...fault4_4.4.22.114-32b9a7c0147a0274963a1f587fe796bc/opt/weka/shm/events/81.shm
       612.00 KiB  612.00 KiB       ~2x   rw-s  ...fault4_4.4.22.114-32b9a7c0147a0274963a1f587fe796bc/opt/weka/shm/events/83.shm
       612.00 KiB  612.00 KiB       ~2x   rw-s  ...fault4_4.4.22.114-32b9a7c0147a0274963a1f587fe796bc/opt/weka/shm/events/82.shm
       508.00 KiB  508.00 KiB      ~46x   r-xp  ....4.22.114-32b9a7c0147a0274963a1f587fe796bc/usr/lib/x86_64-linux-gnu/libm.so.6
      ... 6 more segments
  845837    wekanode --slot 0 --container-name default3       3.53 GiB    104.50 MiB      6.65 MiB    132.00 KiB      2.21 GiB    298.75 MiB           0 B           0 B           0 B           0 B           0 B           0 B
              RSS      Shared  ~sharers  perms  path
       161.32 MiB  161.29 MiB      ~20x   r--p  ...data/agent/default3_4.4.22.114-32b9a7c0147a0274963a1f587fe796bc/weka/wekanode
       101.98 MiB  101.98 MiB      ~20x   r-xp  ...data/agent/default3_4.4.22.114-32b9a7c0147a0274963a1f587fe796bc/weka/wekanode
        32.52 MiB   24.58 MiB       ~2x   rw-s  ...b9a7c0147a0274963a1f587fe796bc/opt/weka/shm/wekanode_slot0_main.v3.trace_area
        32.52 MiB    4.19 MiB       ~1x   rw-s  ...a7c0147a0274963a1f587fe796bc/opt/weka/shm/wekanode_slot0_shared.v3.trace_area
         1.53 MiB    1.53 MiB      ~92x   r-xp  ....4.22.114-32b9a7c0147a0274963a1f587fe796bc/usr/lib/x86_64-linux-gnu/libc.so.6
         1.20 MiB    1.20 MiB       ~2x   rw-s  ...fault3_4.4.22.114-32b9a7c0147a0274963a1f587fe796bc/opt/weka/shm/events/60.shm
       612.00 KiB  612.00 KiB       ~2x   rw-s  ...fault3_4.4.22.114-32b9a7c0147a0274963a1f587fe796bc/opt/weka/shm/events/62.shm
       612.00 KiB  612.00 KiB       ~2x   rw-s  ...fault3_4.4.22.114-32b9a7c0147a0274963a1f587fe796bc/opt/weka/shm/events/61.shm
       612.00 KiB  612.00 KiB       ~2x   rw-s  ...fault3_4.4.22.114-32b9a7c0147a0274963a1f587fe796bc/opt/weka/shm/events/63.shm
       508.00 KiB  508.00 KiB      ~46x   r-xp  ....4.22.114-32b9a7c0147a0274963a1f587fe796bc/usr/lib/x86_64-linux-gnu/libm.so.6
      ... 6 more segments
  845825    wekanode --slot 0 --container-name default1       3.46 GiB    104.50 MiB      4.58 MiB    132.00 KiB      2.14 GiB    288.75 MiB           0 B           0 B           0 B           0 B           0 B           0 B
              RSS      Shared  ~sharers  perms  path
       161.32 MiB  161.29 MiB      ~20x   r--p  ...data/agent/default1_4.4.22.114-32b9a7c0147a0274963a1f587fe796bc/weka/wekanode
       101.98 MiB  101.98 MiB      ~20x   r-xp  ...data/agent/default1_4.4.22.114-32b9a7c0147a0274963a1f587fe796bc/weka/wekanode
        32.52 MiB   15.58 MiB       ~1x   rw-s  ...b9a7c0147a0274963a1f587fe796bc/opt/weka/shm/wekanode_slot0_main.v3.trace_area
        32.52 MiB    3.19 MiB       ~1x   rw-s  ...a7c0147a0274963a1f587fe796bc/opt/weka/shm/wekanode_slot0_shared.v3.trace_area
         1.53 MiB    1.53 MiB      ~92x   r-xp  ....4.22.114-32b9a7c0147a0274963a1f587fe796bc/usr/lib/x86_64-linux-gnu/libc.so.6
         1.20 MiB    1.20 MiB       ~2x   rw-s  ...fault1_4.4.22.114-32b9a7c0147a0274963a1f587fe796bc/opt/weka/shm/events/20.shm
       612.00 KiB  612.00 KiB       ~2x   rw-s  ...fault1_4.4.22.114-32b9a7c0147a0274963a1f587fe796bc/opt/weka/shm/events/21.shm
       612.00 KiB  612.00 KiB       ~2x   rw-s  ...fault1_4.4.22.114-32b9a7c0147a0274963a1f587fe796bc/opt/weka/shm/events/23.shm
       612.00 KiB  612.00 KiB       ~2x   rw-s  ...fault1_4.4.22.114-32b9a7c0147a0274963a1f587fe796bc/opt/weka/shm/events/22.shm
       508.00 KiB  508.00 KiB      ~46x   r-xp  ....4.22.114-32b9a7c0147a0274963a1f587fe796bc/usr/lib/x86_64-linux-gnu/libm.so.6
      ... 6 more segments
  845830    wekanode --slot 0 --container-name default2       3.46 GiB    104.50 MiB      4.10 MiB    132.00 KiB      2.14 GiB    286.75 MiB           0 B           0 B           0 B           0 B           0 B           0 B
              RSS      Shared  ~sharers  perms  path
       161.32 MiB  161.29 MiB      ~20x   r--p  ...data/agent/default2_4.4.22.114-32b9a7c0147a0274963a1f587fe796bc/weka/wekanode
       101.98 MiB  101.98 MiB      ~20x   r-xp  ...data/agent/default2_4.4.22.114-32b9a7c0147a0274963a1f587fe796bc/weka/wekanode
        32.52 MiB   13.58 MiB       ~1x   rw-s  ...b9a7c0147a0274963a1f587fe796bc/opt/weka/shm/wekanode_slot0_main.v3.trace_area
        32.52 MiB    3.19 MiB       ~1x   rw-s  ...a7c0147a0274963a1f587fe796bc/opt/weka/shm/wekanode_slot0_shared.v3.trace_area
         1.53 MiB    1.53 MiB      ~92x   r-xp  ....4.22.114-32b9a7c0147a0274963a1f587fe796bc/usr/lib/x86_64-linux-gnu/libc.so.6
         1.20 MiB    1.20 MiB       ~2x   rw-s  ...fault2_4.4.22.114-32b9a7c0147a0274963a1f587fe796bc/opt/weka/shm/events/40.shm
       612.00 KiB  612.00 KiB       ~2x   rw-s  ...fault2_4.4.22.114-32b9a7c0147a0274963a1f587fe796bc/opt/weka/shm/events/43.shm
       612.00 KiB  612.00 KiB       ~2x   rw-s  ...fault2_4.4.22.114-32b9a7c0147a0274963a1f587fe796bc/opt/weka/shm/events/41.shm
       612.00 KiB  612.00 KiB       ~2x   rw-s  ...fault2_4.4.22.114-32b9a7c0147a0274963a1f587fe796bc/opt/weka/shm/events/42.shm
       508.00 KiB  508.00 KiB      ~46x   r-xp  ....4.22.114-32b9a7c0147a0274963a1f587fe796bc/usr/lib/x86_64-linux-gnu/libm.so.6
      ... 6 more segments
  845845    wekanode --slot 0 --container-name default0       3.46 GiB    104.50 MiB      4.57 MiB    132.00 KiB      2.14 GiB    298.75 MiB           0 B           0 B           0 B           0 B           0 B           0 B
              RSS      Shared  ~sharers  perms  path
       161.32 MiB  161.29 MiB      ~20x   r--p  ...data/agent/default0_4.4.22.114-32b9a7c0147a0274963a1f587fe796bc/weka/wekanode
       101.98 MiB  101.98 MiB      ~20x   r-xp  ...data/agent/default0_4.4.22.114-32b9a7c0147a0274963a1f587fe796bc/weka/wekanode
        32.52 MiB   24.58 MiB       ~2x   rw-s  ...b9a7c0147a0274963a1f587fe796bc/opt/weka/shm/wekanode_slot0_main.v3.trace_area
        32.52 MiB    4.19 MiB       ~1x   rw-s  ...a7c0147a0274963a1f587fe796bc/opt/weka/shm/wekanode_slot0_shared.v3.trace_area
         1.53 MiB    1.53 MiB      ~92x   r-xp  ....4.22.114-32b9a7c0147a0274963a1f587fe796bc/usr/lib/x86_64-linux-gnu/libc.so.6
         1.20 MiB    1.20 MiB       ~2x   rw-s  ...efault0_4.4.22.114-32b9a7c0147a0274963a1f587fe796bc/opt/weka/shm/events/0.shm
       612.00 KiB  612.00 KiB       ~2x   rw-s  ...efault0_4.4.22.114-32b9a7c0147a0274963a1f587fe796bc/opt/weka/shm/events/2.shm
       612.00 KiB  612.00 KiB       ~2x   rw-s  ...efault0_4.4.22.114-32b9a7c0147a0274963a1f587fe796bc/opt/weka/shm/events/3.shm
       612.00 KiB  612.00 KiB       ~2x   rw-s  ...efault0_4.4.22.114-32b9a7c0147a0274963a1f587fe796bc/opt/weka/shm/events/1.shm
       508.00 KiB  508.00 KiB      ~46x   r-xp  ....4.22.114-32b9a7c0147a0274963a1f587fe796bc/usr/lib/x86_64-linux-gnu/libm.so.6
      ... 6 more segments
  ... 146 more (use --top 0 for all)

Per-container summary (grouped by PID namespace / cgroup)
  Container                      Procs           RSS          Code          Heap         Stack      AnonData        Shared          Swap       ExeSwap      FileSwap      THP/code      THP/data       HugeTLB
  weka:default3                     22     11.78 GiB    568.57 MiB    153.23 MiB      2.22 MiB      8.53 GiB      1.44 GiB           0 B     56.13 MiB     16.38 GiB           0 B           0 B     15.84 GiB
  weka:default4                     22     11.77 GiB    568.17 MiB    143.91 MiB      2.23 MiB      8.54 GiB      1.41 GiB           0 B     56.53 MiB     16.39 GiB           0 B           0 B     15.84 GiB
  weka:default0                     22     11.70 GiB    567.63 MiB    141.34 MiB      2.21 MiB      8.45 GiB      1.44 GiB           0 B     57.07 MiB     16.38 GiB           0 B           0 B     15.84 GiB
  weka:default2                     22     11.66 GiB    567.98 MiB    141.55 MiB      2.21 MiB      8.43 GiB      1.40 GiB           0 B     56.71 MiB     16.40 GiB           0 B           0 B     15.84 GiB
  weka:default1                     22     11.64 GiB    567.37 MiB    144.41 MiB      2.23 MiB      8.41 GiB      1.41 GiB           0 B     57.33 MiB     16.39 GiB           0 B           0 B     15.84 GiB
  system.slice                      24    538.25 MiB    135.05 MiB     28.07 MiB    888.00 KiB    159.80 MiB    215.41 MiB           0 B    108.44 MiB    179.67 MiB           0 B           0 B           0 B
  user.slice                        16    173.49 MiB     62.82 MiB     45.68 MiB      2.33 MiB     24.98 MiB     91.50 MiB           0 B     32.63 MiB     46.46 MiB           0 B           0 B           0 B
  system                             1     14.00 MiB      6.05 MiB      3.29 MiB     52.00 KiB    176.00 KiB      9.36 MiB           0 B      5.90 MiB      2.18 MiB           0 B           0 B           0 B

HugeTLB (hugetlbfs) users — Private / Shared
  PID       Command                                              Private          Shared           Total
  847602    wekanode --slot 2 --container-name default3        13.01 GiB             0 B       13.01 GiB
  848861    wekanode --slot 2 --container-name default2        13.01 GiB             0 B       13.01 GiB
  847090    wekanode --slot 2 --container-name default4        13.01 GiB             0 B       13.01 GiB
  848842    wekanode --slot 2 --container-name default0        13.01 GiB             0 B       13.01 GiB
  848838    wekanode --slot 2 --container-name default1        13.01 GiB             0 B       13.01 GiB

Kernel Direct Map (physical memory mapped by page size)
  DirectMap4k:        9.26 GiB
  DirectMap2M:      116.38 GiB
  DirectMap1G:      388.00 GiB
```

### 4. Per-container rollup (`--containers`, or automatic)

`memsleuth` buckets every process by its cgroup path (walking every hierarchy, including named v1 entries like `name=weka` that some runtimes use to record the container id). Priority:

1. `/container/<runtime>/<id>[/...]` — custom cgroup layouts (e.g. Weka: `weka:default0`).
2. Known runtime cgroup patterns — docker, podman/libpod, kubepods, crio, containerd, lxc, nspawn.
3. `/system.slice/*` → single `system.slice` bucket.
4. `/user.slice/*` → single `user.slice` bucket.
5. Everything else → `system`.

A separate PID namespace alone is **not** treated as a container — browser sandboxes each create their own and would flood the table.

The per-container summary prints automatically whenever classification produced more than one bucket. `--containers` additionally regroups the per-process listing under each bucket's header. All container numbers are **summed from our own smaps data** — memsleuth never reads cgroup memory accounting, so the numbers don't depend on in-container instrumentation and stay consistent with the per-process view.

```text
Memory
               total        used        free      shared    buff/cache     available
Mem:      503.45 GiB  140.96 GiB  315.26 GiB    2.38 GiB     47.22 GiB    359.21 GiB
Swap:       8.00 GiB         0 B    8.00 GiB

HugeTLB Pages (explicit hugepage pools)
         Size    Total     Free     Rsvd  Surplus  Overcmt     Used     Mem Used     Mem Free
     2.00 MiB     7260        0        0        0        0     7260    14.18 GiB          0 B
     1.00 GiB       65        0        0        0        0       65    65.00 GiB          0 B
  Pool total: 79.18 GiB  used: 79.18 GiB  free: 0 B

Per-process memory detail (grouped by container)

  [weka:default3]  procs=22  RSS=11.78 GiB  swap=0 B  shared=1.44 GiB  hugetlb=15.84 GiB
    PID       Command                                                RSS          Code          Heap         Stack      AnonData        Shared          Swap       ExeSwap      FileSwap      THP/code      THP/data       HugeTLB
    845837    wekanode --slot 0 --container-name default3       3.53 GiB    104.50 MiB      6.65 MiB    132.00 KiB      2.21 GiB    298.75 MiB           0 B           0 B           0 B           0 B           0 B           0 B
    847602    wekanode --slot 2 --container-name default3       3.40 GiB    117.28 MiB     50.80 MiB    132.00 KiB      2.90 GiB    319.50 MiB           0 B           0 B     13.01 GiB           0 B           0 B     13.01 GiB
    847598    wekanode --slot 1 --container-name default3       2.18 GiB    117.28 MiB     21.00 MiB    132.00 KiB      1.54 GiB    298.50 MiB           0 B           0 B      1.43 GiB           0 B           0 B      1.38 GiB
    847593    wekanode --slot 3 --container-name default3       2.03 GiB    117.28 MiB     21.00 MiB    132.00 KiB      1.56 GiB    303.64 MiB           0 B           0 B      1.45 GiB           0 B           0 B      1.45 GiB
    936244    trace-dumper --histogram-interval 60            292.92 MiB      3.97 MiB    560.00 KiB     24.00 KiB    172.00 MiB    113.09 MiB           0 B    808.00 KiB    389.29 MiB           0 B           0 B           0 B
    844953    node ./apiv2-server.js --max-old-space-size…     76.41 MiB     44.37 MiB      8.17 MiB     76.00 KiB     22.32 MiB     45.64 MiB           0 B     35.20 MiB    900.00 KiB           0 B           0 B           0 B
    844882    trace-server --socket-address /var/run/trac…     66.54 MiB      4.38 MiB    136.00 KiB     20.00 KiB     59.35 MiB      6.68 MiB           0 B      3.05 MiB     11.03 MiB           0 B           0 B           0 B
    844875    python3.12 /usr/local/bin/supervisord -n -c…     32.68 MiB      7.34 MiB      8.28 MiB     76.00 KiB     11.38 MiB     11.13 MiB           0 B      2.07 MiB      3.23 MiB           0 B           0 B           0 B
    844877    python3-weka /weka/scripts/events.py --rpcb…     30.75 MiB      7.14 MiB      8.02 MiB     80.00 KiB      9.84 MiB     10.98 MiB           0 B      2.36 MiB      3.23 MiB           0 B           0 B           0 B
    844888    python3-weka /weka/scripts/rotate_files.py       28.39 MiB      6.30 MiB      7.85 MiB     80.00 KiB      8.84 MiB      9.86 MiB           0 B      2.66 MiB      3.29 MiB           0 B           0 B           0 B
    ... 12 more (use --top 0 for all)

  [weka:default4]  procs=22  RSS=11.77 GiB  swap=0 B  shared=1.42 GiB  hugetlb=15.84 GiB
    PID       Command                                                RSS          Code          Heap         Stack      AnonData        Shared          Swap       ExeSwap      FileSwap      THP/code      THP/data       HugeTLB
    845836    wekanode --slot 0 --container-name default4       3.55 GiB    104.50 MiB      7.08 MiB    132.00 KiB      2.22 GiB    293.79 MiB           0 B           0 B           0 B           0 B           0 B           0 B
    847090    wekanode --slot 2 --container-name default4       3.40 GiB    117.28 MiB     41.52 MiB    132.00 KiB      2.90 GiB    319.50 MiB           0 B           0 B     13.01 GiB           0 B           0 B     13.01 GiB
    847066    wekanode --slot 1 --container-name default4       2.18 GiB    117.28 MiB     21.00 MiB    132.00 KiB      1.54 GiB    294.52 MiB           0 B           0 B      1.43 GiB           0 B           0 B      1.38 GiB
    847023    wekanode --slot 3 --container-name default4       2.03 GiB    117.28 MiB     21.00 MiB    132.00 KiB      1.56 GiB    299.68 MiB           0 B           0 B      1.45 GiB           0 B           0 B      1.45 GiB
    946766    trace-dumper --histogram-interval 60            279.69 MiB      3.89 MiB    536.00 KiB     20.00 KiB    170.99 MiB    100.56 MiB           0 B    892.00 KiB    401.39 MiB           0 B           0 B           0 B
    845625    node ./apiv2-server.js --max-old-space-size…     76.66 MiB     44.37 MiB      7.82 MiB     80.00 KiB     22.92 MiB     45.64 MiB           0 B     35.20 MiB    900.00 KiB           0 B           0 B           0 B
    845555    trace-server --socket-address /var/run/trac…     66.53 MiB      4.38 MiB    136.00 KiB     16.00 KiB     59.35 MiB      6.68 MiB           0 B      3.05 MiB     11.03 MiB           0 B           0 B           0 B
    845492    python3.12 /usr/local/bin/supervisord -n -c…     32.46 MiB      7.09 MiB      8.28 MiB     80.00 KiB     11.38 MiB     10.91 MiB           0 B      2.31 MiB      3.22 MiB           0 B           0 B           0 B
    845550    python3-weka /weka/scripts/events.py --rpcb…     31.00 MiB      7.37 MiB      8.02 MiB     80.00 KiB      9.84 MiB     11.23 MiB           0 B      2.13 MiB      3.22 MiB           0 B           0 B           0 B
    845563    python3-weka /weka/scripts/rotate_files.py       28.22 MiB      6.14 MiB      7.85 MiB     76.00 KiB      8.86 MiB      9.68 MiB           0 B      2.83 MiB      3.30 MiB           0 B           0 B           0 B
    ... 12 more (use --top 0 for all)

  [weka:default0]  procs=22  RSS=11.70 GiB  swap=0 B  shared=1.44 GiB  hugetlb=15.84 GiB
    PID       Command                                                RSS          Code          Heap         Stack      AnonData        Shared          Swap       ExeSwap      FileSwap      THP/code      THP/data       HugeTLB
    845845    wekanode --slot 0 --container-name default0       3.46 GiB    104.50 MiB      4.57 MiB    132.00 KiB      2.14 GiB    299.75 MiB           0 B           0 B           0 B           0 B           0 B           0 B
    848842    wekanode --slot 2 --container-name default0       3.40 GiB    117.28 MiB     40.86 MiB    132.00 KiB      2.90 GiB    319.50 MiB           0 B           0 B     13.01 GiB           0 B           0 B     13.01 GiB
    848839    wekanode --slot 1 --container-name default0       2.18 GiB    117.28 MiB     21.00 MiB    132.00 KiB      1.54 GiB    298.50 MiB           0 B           0 B      1.43 GiB           0 B           0 B      1.38 GiB
    848879    wekanode --slot 3 --container-name default0       2.03 GiB    117.28 MiB     21.00 MiB    132.00 KiB      1.56 GiB    303.64 MiB           0 B           0 B      1.45 GiB           0 B           0 B      1.45 GiB
    934977    trace-dumper --histogram-interval 60            293.67 MiB      3.87 MiB    536.00 KiB     16.00 KiB    171.72 MiB    114.27 MiB           0 B    908.00 KiB    388.12 MiB           0 B           0 B           0 B
    843121    node ./apiv2-server.js --max-old-space-size…     76.78 MiB     44.26 MiB      8.51 MiB     80.00 KiB     22.48 MiB     45.49 MiB           0 B     35.31 MiB    920.00 KiB           0 B           0 B           0 B
    843052    trace-server --socket-address /var/run/trac…     66.62 MiB      4.43 MiB    136.00 KiB     24.00 KiB     59.35 MiB      6.76 MiB           0 B      3.00 MiB     11.00 MiB           0 B           0 B           0 B
    842936    python3.12 /usr/local/bin/supervisord -n -c…     32.54 MiB      7.23 MiB      8.28 MiB     84.00 KiB     11.38 MiB     10.98 MiB           0 B      2.17 MiB      3.28 MiB           0 B           0 B           0 B
    843047    python3-weka /weka/scripts/events.py --rpcb…     30.78 MiB      7.12 MiB      8.02 MiB     76.00 KiB      9.84 MiB     10.99 MiB           0 B      2.38 MiB      3.19 MiB           0 B           0 B           0 B
    843060    python3-weka /weka/scripts/rotate_files.py       27.70 MiB      6.10 MiB      7.85 MiB     76.00 KiB      8.42 MiB      9.60 MiB           0 B      2.87 MiB      3.34 MiB           0 B           0 B           0 B
    ... 12 more (use --top 0 for all)

  [weka:default2]  procs=22  RSS=11.66 GiB  swap=0 B  shared=1.40 GiB  hugetlb=15.84 GiB
    PID       Command                                                RSS          Code          Heap         Stack      AnonData        Shared          Swap       ExeSwap      FileSwap      THP/code      THP/data       HugeTLB
    845830    wekanode --slot 0 --container-name default2       3.46 GiB    104.50 MiB      4.10 MiB    132.00 KiB      2.14 GiB    287.75 MiB           0 B           0 B           0 B           0 B           0 B           0 B
    848861    wekanode --slot 2 --container-name default2       3.40 GiB    117.28 MiB     42.19 MiB    132.00 KiB      2.90 GiB    319.50 MiB           0 B           0 B     13.01 GiB           0 B           0 B     13.01 GiB
    848846    wekanode --slot 1 --container-name default2       2.18 GiB    117.28 MiB     21.00 MiB    132.00 KiB      1.54 GiB    294.56 MiB           0 B           0 B      1.43 GiB           0 B           0 B      1.38 GiB
    848852    wekanode --slot 3 --container-name default2       2.03 GiB    117.28 MiB     21.00 MiB    132.00 KiB      1.56 GiB    296.64 MiB           0 B           0 B      1.45 GiB           0 B           0 B      1.45 GiB
    946703    trace-dumper --histogram-interval 60            248.88 MiB      3.88 MiB    536.00 KiB     24.00 KiB    150.15 MiB     90.64 MiB           0 B    904.00 KiB    411.36 MiB           0 B           0 B           0 B
    844325    node ./apiv2-server.js --max-old-space-size…     76.00 MiB     44.29 MiB      7.71 MiB     80.00 KiB     22.48 MiB     45.53 MiB           0 B     35.28 MiB    928.00 KiB           0 B           0 B           0 B
    844253    trace-server --socket-address /var/run/trac…     66.54 MiB      4.38 MiB    136.00 KiB     20.00 KiB     59.35 MiB      6.68 MiB           0 B      3.05 MiB     11.03 MiB           0 B           0 B           0 B
    844207    python3.12 /usr/local/bin/supervisord -n -c…     32.61 MiB      7.20 MiB      8.28 MiB     80.00 KiB     11.38 MiB     11.06 MiB           0 B      2.21 MiB      3.17 MiB           0 B           0 B           0 B
    844248    python3-weka /weka/scripts/events.py --rpcb…     30.84 MiB      7.25 MiB      8.02 MiB     80.00 KiB      9.84 MiB     11.05 MiB           0 B      2.25 MiB      3.25 MiB           0 B           0 B           0 B
    844261    python3-weka /weka/scripts/rotate_files.py       29.54 MiB      6.30 MiB      7.85 MiB     80.00 KiB     10.00 MiB      9.86 MiB           0 B      2.66 MiB      3.29 MiB           0 B           0 B           0 B
    ... 12 more (use --top 0 for all)

  [weka:default1]  procs=22  RSS=11.64 GiB  swap=0 B  shared=1.41 GiB  hugetlb=15.84 GiB
    PID       Command                                                RSS          Code          Heap         Stack      AnonData        Shared          Swap       ExeSwap      FileSwap      THP/code      THP/data       HugeTLB
    845825    wekanode --slot 0 --container-name default1       3.46 GiB    104.50 MiB      4.58 MiB    132.00 KiB      2.14 GiB    288.75 MiB           0 B           0 B           0 B           0 B           0 B           0 B
    848838    wekanode --slot 2 --container-name default1       3.37 GiB    117.28 MiB     44.64 MiB    132.00 KiB      2.88 GiB    319.50 MiB           0 B           0 B     13.01 GiB           0 B           0 B     13.01 GiB
    848845    wekanode --slot 1 --container-name default1       2.18 GiB    117.28 MiB     21.00 MiB    132.00 KiB      1.54 GiB    294.54 MiB           0 B           0 B      1.43 GiB           0 B           0 B      1.38 GiB
    848840    wekanode --slot 3 --container-name default1       2.03 GiB    117.28 MiB     21.00 MiB    132.00 KiB      1.56 GiB    300.70 MiB           0 B           0 B      1.45 GiB           0 B           0 B      1.45 GiB
    945461    trace-dumper --histogram-interval 60            255.00 MiB      3.89 MiB    536.00 KiB     24.00 KiB    150.29 MiB     96.65 MiB           0 B    892.00 KiB    405.38 MiB           0 B           0 B           0 B
    843692    node ./apiv2-server.js --max-old-space-size…     78.11 MiB     44.16 MiB      7.71 MiB     84.00 KiB     24.68 MiB     45.43 MiB           0 B     35.41 MiB    884.00 KiB           0 B           0 B           0 B
    843623    trace-server --socket-address /var/run/trac…     66.58 MiB      4.41 MiB    136.00 KiB     20.00 KiB     59.35 MiB      6.72 MiB           0 B      3.02 MiB     11.02 MiB           0 B           0 B           0 B
    843612    python3.12 /usr/local/bin/supervisord -n -c…     32.57 MiB      7.24 MiB      8.28 MiB     76.00 KiB     11.38 MiB     11.02 MiB           0 B      2.17 MiB      3.25 MiB           0 B           0 B           0 B
    843618    python3-weka /weka/scripts/events.py --rpcb…     30.75 MiB      7.14 MiB      8.02 MiB     80.00 KiB      9.84 MiB     10.98 MiB           0 B      2.36 MiB      3.23 MiB           0 B           0 B           0 B
    843631    python3-weka /weka/scripts/rotate_files.py       27.92 MiB      6.10 MiB      7.85 MiB     80.00 KiB      8.61 MiB      9.62 MiB           0 B      2.87 MiB      3.32 MiB           0 B           0 B           0 B
    ... 12 more (use --top 0 for all)

  [system.slice]  procs=24  RSS=538.42 MiB  swap=0 B  shared=215.40 MiB
    PID       Command                                                RSS          Code          Heap         Stack      AnonData        Shared          Swap       ExeSwap      FileSwap      THP/code      THP/data       HugeTLB
    3722      weka --agent                                    172.46 MiB     28.40 MiB      2.07 MiB     16.00 KiB    133.09 MiB      2.58 MiB           0 B      3.61 MiB      1.88 MiB           0 B           0 B           0 B
    2185      systemd-journald                                127.62 MiB      4.50 MiB    764.00 KiB     80.00 KiB    128.00 KiB     83.74 MiB           0 B      6.43 MiB     63.09 MiB           0 B           0 B           0 B
    40040     fwupd                                            51.36 MiB     13.77 MiB     11.05 MiB     44.00 KiB    680.00 KiB     12.55 MiB           0 B     15.22 MiB     41.17 MiB           0 B           0 B           0 B
    2239      multipathd -d -s                                 27.13 MiB      6.20 MiB    788.00 KiB    132.00 KiB     17.37 MiB      5.06 MiB           0 B           0 B           0 B           0 B           0 B           0 B
    3671      python3 /usr/share/unattended-upgrades/unat…     22.82 MiB      8.50 MiB      2.77 MiB     80.00 KiB      4.71 MiB     11.02 MiB           0 B      4.28 MiB      6.73 MiB           0 B           0 B           0 B
    3338      udisksd                                          14.77 MiB      7.19 MiB      1.59 MiB     80.00 KiB    492.00 KiB      8.49 MiB           0 B      8.63 MiB      3.62 MiB           0 B           0 B           0 B
    3158      systemd-resolved                                 12.80 MiB      7.46 MiB      1.28 MiB     36.00 KiB     88.00 KiB      9.68 MiB           0 B      4.32 MiB      3.60 MiB           0 B           0 B           0 B
    3395      ModemManager                                     12.57 MiB      6.18 MiB    856.00 KiB     12.00 KiB    224.00 KiB      7.77 MiB           0 B      6.67 MiB      3.46 MiB           0 B           0 B           0 B
    2315      systemd-networkd                                  9.94 MiB      5.43 MiB    316.00 KiB     36.00 KiB     84.00 KiB      7.48 MiB           0 B      6.20 MiB      5.16 MiB           0 B           0 B           0 B
    2268      systemd-udevd                                     9.40 MiB      3.12 MiB      3.58 MiB     20.00 KiB     68.00 KiB      3.64 MiB           0 B      4.19 MiB     16.23 MiB           0 B           0 B           0 B
    ... 14 more (use --top 0 for all)

  [user.slice]  procs=16  RSS=172.53 MiB  swap=0 B  shared=91.51 MiB
    PID       Command                                                RSS          Code          Heap         Stack      AnonData        Shared          Swap       ExeSwap      FileSwap      THP/code      THP/data       HugeTLB
    962117    python3 ./memsleuth.py --containers --top 10     41.37 MiB      4.38 MiB     11.13 MiB     80.00 KiB     21.99 MiB      6.89 MiB           0 B    904.00 KiB      4.98 MiB           0 B           0 B           0 B
    440786    6                                                11.70 MiB      6.24 MiB    992.00 KiB    268.00 KiB    904.00 KiB      8.80 MiB           0 B      2.04 MiB      1.57 MiB           0 B           0 B           0 B
    3367069   systemd --user                                   11.61 MiB      6.01 MiB      1.23 MiB     24.00 KiB     92.00 KiB      9.18 MiB           0 B      5.94 MiB      2.37 MiB           0 B           0 B           0 B
    613651    sshd: root@notty                                 11.35 MiB      6.32 MiB      1.30 MiB    272.00 KiB     96.00 KiB      8.92 MiB           0 B      1.96 MiB      1.54 MiB           0 B           0 B           0 B
    427123    0                                                11.04 MiB      6.32 MiB    988.00 KiB    276.00 KiB     96.00 KiB      8.94 MiB           0 B      1.96 MiB      1.52 MiB           0 B           0 B           0 B
    4042096   5                                                10.53 MiB      5.96 MiB    848.00 KiB    272.00 KiB     96.00 KiB      8.57 MiB           0 B      2.32 MiB      1.53 MiB           0 B           0 B           0 B
    3417690   tmate -f /tmp/tmate.conf                          8.48 MiB      5.25 MiB    940.00 KiB    124.00 KiB     88.00 KiB      6.55 MiB           0 B      1.49 MiB      4.67 MiB           0 B           0 B           0 B
    3417691   -bash                                             8.29 MiB      2.58 MiB      4.12 MiB    112.00 KiB     72.00 KiB      3.88 MiB           0 B    132.00 KiB      2.66 MiB           0 B           0 B           0 B
    440895    bash --login --posix                              8.23 MiB      2.58 MiB      4.00 MiB    108.00 KiB     72.00 KiB      3.95 MiB           0 B    132.00 KiB      2.59 MiB           0 B           0 B           0 B
    4042197   -bash                                             8.23 MiB      2.58 MiB      4.11 MiB    108.00 KiB     72.00 KiB      3.84 MiB           0 B    132.00 KiB      2.70 MiB           0 B           0 B           0 B
    ... 6 more (use --top 0 for all)

  [system]  procs=1  RSS=14.00 MiB  swap=0 B  shared=9.36 MiB
    PID       Command                                                RSS          Code          Heap         Stack      AnonData        Shared          Swap       ExeSwap      FileSwap      THP/code      THP/data       HugeTLB
    1         init                                             14.00 MiB      6.05 MiB      3.29 MiB     52.00 KiB    176.00 KiB      9.36 MiB           0 B      5.90 MiB      2.18 MiB           0 B           0 B           0 B

Per-container summary (grouped by PID namespace / cgroup)
  Container                      Procs           RSS          Code          Heap         Stack      AnonData        Shared          Swap       ExeSwap      FileSwap      THP/code      THP/data       HugeTLB
  weka:default3                     22     11.78 GiB    568.55 MiB    153.23 MiB      2.22 MiB      8.53 GiB      1.44 GiB           0 B     56.15 MiB     16.38 GiB           0 B           0 B     15.84 GiB
  weka:default4                     22     11.77 GiB    568.19 MiB    143.91 MiB      2.23 MiB      8.54 GiB      1.42 GiB           0 B     56.51 MiB     16.39 GiB           0 B           0 B     15.84 GiB
  weka:default0                     22     11.70 GiB    567.63 MiB    141.34 MiB      2.21 MiB      8.45 GiB      1.44 GiB           0 B     57.07 MiB     16.38 GiB           0 B           0 B     15.84 GiB
  weka:default2                     22     11.66 GiB    567.99 MiB    141.55 MiB      2.21 MiB      8.43 GiB      1.40 GiB           0 B     56.71 MiB     16.40 GiB           0 B           0 B     15.84 GiB
  weka:default1                     22     11.64 GiB    567.37 MiB    144.41 MiB      2.23 MiB      8.41 GiB      1.41 GiB           0 B     57.33 MiB     16.39 GiB           0 B           0 B     15.84 GiB
  system.slice                      24    538.42 MiB    135.05 MiB     28.07 MiB    888.00 KiB    159.80 MiB    215.40 MiB           0 B    108.44 MiB    179.49 MiB           0 B           0 B           0 B
  user.slice                        16    172.53 MiB     62.84 MiB     45.68 MiB      2.33 MiB     24.00 MiB     91.51 MiB           0 B     32.62 MiB     46.46 MiB           0 B           0 B           0 B
  system                             1     14.00 MiB      6.05 MiB      3.29 MiB     52.00 KiB    176.00 KiB      9.36 MiB           0 B      5.90 MiB      2.18 MiB           0 B           0 B           0 B

HugeTLB (hugetlbfs) users — Private / Shared
  PID       Command                                              Private          Shared           Total
  847602    wekanode --slot 2 --container-name default3        13.01 GiB             0 B       13.01 GiB
  848861    wekanode --slot 2 --container-name default2        13.01 GiB             0 B       13.01 GiB
  847090    wekanode --slot 2 --container-name default4        13.01 GiB             0 B       13.01 GiB
  848842    wekanode --slot 2 --container-name default0        13.01 GiB             0 B       13.01 GiB
  848838    wekanode --slot 2 --container-name default1        13.01 GiB             0 B       13.01 GiB
  847023    wekanode --slot 3 --container-name default4         1.45 GiB             0 B        1.45 GiB
  847593    wekanode --slot 3 --container-name default3         1.45 GiB             0 B        1.45 GiB
  848840    wekanode --slot 3 --container-name default1         1.45 GiB             0 B        1.45 GiB
  848852    wekanode --slot 3 --container-name default2         1.45 GiB             0 B        1.45 GiB
  848879    wekanode --slot 3 --container-name default0         1.45 GiB             0 B        1.45 GiB

Kernel Direct Map (physical memory mapped by page size)
  DirectMap4k:        9.26 GiB
  DirectMap2M:      116.38 GiB
  DirectMap1G:      388.00 GiB
```

### Hugepage allocation capacity (always shown)

Right after the hugepage pool table memsleuth answers "could I allocate a hugepage right now, and from which NUMA node?" for every configured pool size.

- **Pool free / Pool total** — reserved in the persistent hugepage pool (`/sys/.../hugepages-*kB/{free,nr}_hugepages` plus surplus). Immediately usable.
- **Buddy safe** — free blocks at the hugepage order in Movable / Reclaimable / CMA migration pools, aggregated from `/proc/pagetypeinfo`. These can be allocated without relocating kernel data. The file is root-only; runs as a regular user render this cell as `needs root`.
- **Buddy max** — free blocks across **all** migration types, from `/proc/buddyinfo` (world-readable). The pages beyond Buddy safe may need compaction/migration and aren't guaranteed.
- **`pool only`** — the hugepage size exceeds `MAX_ORDER × base_page` (typically 1 GiB on x86_64, since MAX_ORDER caps the buddy allocator around 4 MiB). Those pages come only from the persistent pool or `hugetlb_cma=`; the buddy allocator can't produce new ones at runtime.

Larger-order free blocks count for multiple hugepages — an order-K block splits into `2^(K - hp_order)` hugepages of the target size.

```text
<!-- paste: sudo ./memsleuth.py | sed -n '/HugeTLB Pages/,/Transparent/p'  -->
```

### 5. NUMA breakdown (`--numa`)

Splits each hugepage pool by NUMA node. **Combined with `--procs`**, each process row is followed by one `N<id>` sub-row per online NUMA node, breaking down `RSS`, `Code`, `Heap`, `Stack`, `AnonData`, `Shared`, and `HugeTLB` per node (Swap/ExeSwap/FileSwap/THP columns show `—` in sub-rows — they're disk-backed or already accounted in the RSS cells). Numbers come from `/proc/<pid>/numa_maps`, matched to smaps VMAs by start address and scaled by each line's own `kernelpagesize_kB`, so 4 KiB pages, 2 MiB THP, and 1 GiB hugetlb are all handled. The `huge` token on a numa_maps line routes those bytes to HugeTLB instead of RSS so the two don't double-count. Sub-rows are suppressed on single-node hosts.

```text
Memory
               total        used        free      shared    buff/cache     available
Mem:      503.45 GiB  141.04 GiB  315.18 GiB    2.38 GiB     47.22 GiB    359.13 GiB
Swap:       8.00 GiB         0 B    8.00 GiB

HugeTLB Pages (explicit hugepage pools)
         Size    Total     Free     Rsvd  Surplus  Overcmt     Used     Mem Used     Mem Free
     2.00 MiB     7260        0        0        0        0     7260    14.18 GiB          0 B
     1.00 GiB       65        0        0        0        0       65    65.00 GiB          0 B
  Pool total: 79.18 GiB  used: 79.18 GiB  free: 0 B

HugeTLB Pages per NUMA node
  node0
          Size   Total    Free Surplus    Used    Mem Used    Mem Free
      2.00 MiB    3525       0       0    3525    6.88 GiB         0 B
      1.00 GiB       0       0       0       0         0 B         0 B
  node1
          Size   Total    Free Surplus    Used    Mem Used    Mem Free
      2.00 MiB      15       0       0      15   30.00 MiB         0 B
      1.00 GiB      39       0       0      39   39.00 GiB         0 B
  node2
          Size   Total    Free Surplus    Used    Mem Used    Mem Free
      2.00 MiB      25       0       0      25   50.00 MiB         0 B
      1.00 GiB      26       0       0      26   26.00 GiB         0 B
  node3
          Size   Total    Free Surplus    Used    Mem Used    Mem Free
      2.00 MiB    3695       0       0    3695    7.22 GiB         0 B
      1.00 GiB       0       0       0       0         0 B         0 B

Kernel Direct Map (physical memory mapped by page size)
  DirectMap4k:        9.26 GiB
  DirectMap2M:      116.38 GiB
  DirectMap1G:      388.00 GiB
```

### 6. NUMA detailed (`--numa --proc`)

Provides detailed info for the process components on what NUMAs they reside

```text
Memory
               total        used        free      shared    buff/cache     available
Mem:      503.45 GiB  141.15 GiB  314.89 GiB    2.53 GiB     47.41 GiB    359.03 GiB
Swap:       8.00 GiB         0 B    8.00 GiB

HugeTLB Pages (explicit hugepage pools)
         Size    Total     Free     Rsvd  Surplus  Overcmt     Used     Mem Used     Mem Free
     2.00 MiB     7260        0        0        0        0     7260    14.18 GiB          0 B
     1.00 GiB       65        0        0        0        0       65    65.00 GiB          0 B
  Pool total: 79.18 GiB  used: 79.18 GiB  free: 0 B

HugeTLB Pages per NUMA node
  node0
          Size   Total    Free Surplus    Used    Mem Used    Mem Free
      2.00 MiB    3525       0       0    3525    6.88 GiB         0 B
      1.00 GiB       0       0       0       0         0 B         0 B
  node1
          Size   Total    Free Surplus    Used    Mem Used    Mem Free
      2.00 MiB      15       0       0      15   30.00 MiB         0 B
      1.00 GiB      39       0       0      39   39.00 GiB         0 B
  node2
          Size   Total    Free Surplus    Used    Mem Used    Mem Free
      2.00 MiB      25       0       0      25   50.00 MiB         0 B
      1.00 GiB      26       0       0      26   26.00 GiB         0 B
  node3
          Size   Total    Free Surplus    Used    Mem Used    Mem Free
      2.00 MiB    3695       0       0    3695    7.22 GiB         0 B
      1.00 GiB       0       0       0       0         0 B         0 B

Per-process memory detail
  PID       Command                                                RSS          Code          Heap         Stack      AnonData        Shared          Swap       ExeSwap      FileSwap      THP/code      THP/data       HugeTLB
  845836    wekanode --slot 0 --container-name default4       3.55 GiB    104.50 MiB      7.08 MiB    132.00 KiB      2.22 GiB    305.69 MiB           0 B           0 B           0 B           0 B           0 B           0 B
            N0                                                    1.2G          102M             0             0          341M          299M             —             —             —             —             —             0
            N1                                                    1.7G          204K            7M          132K          1.2G            1M             —             —             —             —             —             0
            N2                                                    500K          304K             0             0          132K          368K             —             —             —             —             —             0
            N3                                                    687M            2M          132K             0          682M            5M             —             —             —             —             —             0
  845837    wekanode --slot 0 --container-name default3       3.53 GiB    104.50 MiB      6.65 MiB    132.00 KiB      2.21 GiB    295.79 MiB           0 B           0 B           0 B           0 B           0 B           0 B
            N0                                                    988M          102M          376K             0          104M          291M             —             —             —             —             —             0
            N1                                                    1.1G          204K            4M             0          1.0G          308K             —             —             —             —             —             0
            N2                                                    141M          304K          144K             0          141M          374K             —             —             —             —             —             0
            N3                                                    1.3G            2M            2M          132K          983M            4M             —             —             —             —             —             0
  845825    wekanode --slot 0 --container-name default1       3.46 GiB    104.50 MiB      4.58 MiB    132.00 KiB      2.14 GiB    285.81 MiB           0 B           0 B           0 B           0 B           0 B           0 B
            N0                                                    2.3G          102M            2M          132K          1.3G          264M             —             —             —             —             —             0
            N1                                                    923M          204K            2M             0          856M           16M             —             —             —             —             —             0
            N2                                                      1M          304K          640K             0          388K          369K             —             —             —             —             —             0
            N3                                                    297M            2M          200K             0            2M            5M             —             —             —             —             —             0
  845830    wekanode --slot 0 --container-name default2       3.46 GiB    104.50 MiB      4.10 MiB    132.00 KiB      2.14 GiB    295.79 MiB           0 B           0 B           0 B           0 B           0 B           0 B
            N0                                                    800M          102M          288K             0          208M          289M             —             —             —             —             —             0
            N1                                                    834M          204K            2M            4K          735M            2M             —             —             —             —             —             0
            N2                                                    504K          304K             0             0          132K          368K             —             —             —             —             —             0
            N3                                                    1.9G            2M            2M          128K          1.2G            4M             —             —             —             —             —             0
  845845    wekanode --slot 0 --container-name default0       3.46 GiB    104.50 MiB      4.57 MiB    132.00 KiB      2.14 GiB    294.79 MiB           0 B           0 B           0 B           0 B           0 B           0 B
            N0                                                    1.1G          102M          148K             0          245M          288M             —             —             —             —             —             0
            N1                                                   1003M          204K            1M             0          906M          341K             —             —             —             —             —             0
            N2                                                    1.4G          304K            2M          132K          1.0G            2M             —             —             —             —             —             0
            N3                                                      7M            2M            2M             0             0            5M             —             —             —             —             —             0
  847602    wekanode --slot 2 --container-name default3       3.40 GiB    117.28 MiB     50.80 MiB    132.00 KiB      2.90 GiB    319.50 MiB           0 B           0 B     13.01 GiB           0 B           0 B     13.01 GiB
            N0                                                    264M          102M             0             0             0          264M             —             —             —             —             —             0
            N1                                                    404K          312K             0             0             0          404K             —             —             —             —             —             0
            N2                                                    3.1G           12M           51M          132K          2.9G           51M             —             —             —             —             —         13.0G
            N3                                                      4M            3M             0             0             0            4M             —             —             —             —             —             0
  847090    wekanode --slot 2 --container-name default4       3.40 GiB    117.28 MiB     42.18 MiB    132.00 KiB      2.90 GiB    319.50 MiB           0 B           0 B     13.01 GiB           0 B           0 B     13.01 GiB
            N0                                                    263M          102M             0             0             0          263M             —             —             —             —             —             0
            N1                                                    404K          312K             0             0             0          404K             —             —             —             —             —             0
            N2                                                    3.1G           12M           42M          132K          2.9G           51M             —             —             —             —             —         13.0G
            N3                                                      4M            3M             0             0             0            4M             —             —             —             —             —             0
  848861    wekanode --slot 2 --container-name default2       3.40 GiB    117.28 MiB     42.19 MiB    132.00 KiB      2.90 GiB    319.50 MiB           0 B           0 B     13.01 GiB           0 B           0 B     13.01 GiB
            N0                                                    263M          102M             0             0             0          263M             —             —             —             —             —             0
            N1                                                    3.1G          312K           42M          132K          2.9G           35M             —             —             —             —             —         13.0G
            N2                                                     18M           12M             0             0             0           18M             —             —             —             —             —            2M
            N3                                                      4M            3M             0             0             0            4M             —             —             —             —             —             0
  848842    wekanode --slot 2 --container-name default0       3.40 GiB    117.28 MiB     40.86 MiB    132.00 KiB      2.90 GiB    319.50 MiB           0 B           0 B     13.01 GiB           0 B           0 B     13.01 GiB
            N0                                                    263M          102M             0             0             0          263M             —             —             —             —             —             0
            N1                                                    3.1G          312K           41M          132K          2.9G           34M             —             —             —             —             —         13.0G
            N2                                                     18M           12M             0             0             0           18M             —             —             —             —             —            2M
            N3                                                      4M            3M             0             0             0            4M             —             —             —             —             —             0
  848838    wekanode --slot 2 --container-name default1       3.37 GiB    117.28 MiB     44.64 MiB    132.00 KiB      2.88 GiB    319.50 MiB           0 B           0 B     13.01 GiB           0 B           0 B     13.01 GiB
            N0                                                    263M          102M             0             0             0          263M             —             —             —             —             —             0
            N1                                                    3.1G          312K           45M          132K          2.9G           34M             —             —             —             —             —         13.0G
            N2                                                     18M           12M             0             0             0           18M             —             —             —             —             —            2M
            N3                                                      4M            3M             0             0             0            4M             —             —             —             —             —             0
  847066    wekanode --slot 1 --container-name default4       2.18 GiB    117.28 MiB     21.00 MiB    132.00 KiB      1.54 GiB    299.57 MiB           0 B           0 B      1.43 GiB           0 B           0 B      1.38 GiB
            N0                                                    2.2G          102M           21M          132K          1.5G          277M             —             —             —             —             —          1.4G
            N1                                                    404K          312K             0             0             0          404K             —             —             —             —             —             0
            N2                                                     18M           12M             0             0             0           18M             —             —             —             —             —            2M
            N3                                                      4M            3M             0             0             0            4M             —             —             —             —             —             0
  847598    wekanode --slot 1 --container-name default3       2.18 GiB    117.28 MiB     21.00 MiB    132.00 KiB      1.54 GiB    297.58 MiB           0 B           0 B      1.43 GiB           0 B           0 B      1.38 GiB
            N0                                                    2.2G          102M           21M          132K          1.5G          276M             —             —             —             —             —          1.4G
            N1                                                    404K          312K             0             0             0          404K             —             —             —             —             —             0
            N2                                                     18M           12M             0             0             0           18M             —             —             —             —             —            2M
            N3                                                      4M            3M             0             0             0            4M             —             —             —             —             —             0
  848839    wekanode --slot 1 --container-name default0       2.18 GiB    117.28 MiB     21.00 MiB    132.00 KiB      1.54 GiB    296.58 MiB           0 B           0 B      1.43 GiB           0 B           0 B      1.38 GiB
            N0                                                    2.2G          102M           21M          132K          1.5G          274M             —             —             —             —             —          1.4G
            N1                                                    408K          312K             0             0             0          408K             —             —             —             —             —             0
            N2                                                     18M           12M             0             0             0           18M             —             —             —             —             —            2M
            N3                                                      4M            3M             0             0             0            4M             —             —             —             —             —             0
  848845    wekanode --slot 1 --container-name default1       2.18 GiB    117.28 MiB     21.00 MiB    132.00 KiB      1.54 GiB    292.60 MiB           0 B           0 B      1.43 GiB           0 B           0 B      1.38 GiB
            N0                                                    2.2G          102M           21M          132K          1.5G          270M             —             —             —             —             —          1.4G
            N1                                                    472K          312K             0             0             0          472K             —             —             —             —             —             0
            N2                                                     18M           12M             0             0             0           18M             —             —             —             —             —            2M
            N3                                                      4M            3M             0             0             0            4M             —             —             —             —             —             0
  848846    wekanode --slot 1 --container-name default2       2.18 GiB    117.28 MiB     21.00 MiB    132.00 KiB      1.54 GiB    296.58 MiB           0 B           0 B      1.43 GiB           0 B           0 B      1.38 GiB
            N0                                                    2.2G          102M           21M          132K          1.5G          274M             —             —             —             —             —          1.4G
            N1                                                   1020K          312K             0             0             0         1020K             —             —             —             —             —             0
            N2                                                     18M           12M             0             0             0           18M             —             —             —             —             —            2M
            N3                                                      4M            3M             0             0             0            4M             —             —             —             —             —             0
  ... 135 more (use --top 0 for all)

Per-container summary (grouped by PID namespace / cgroup)
  Container                      Procs           RSS          Code          Heap         Stack      AnonData        Shared          Swap       ExeSwap      FileSwap      THP/code      THP/data       HugeTLB
  weka:default4                     22     11.81 GiB    568.13 MiB    144.59 MiB      2.23 MiB      8.54 GiB      1.47 GiB           0 B     56.57 MiB     16.36 GiB           0 B           0 B     15.84 GiB
  weka:default3                     22     11.78 GiB    568.38 MiB    153.20 MiB      2.22 MiB      8.53 GiB      1.43 GiB           0 B     56.32 MiB     16.38 GiB           0 B           0 B     15.84 GiB
  weka:default0                     22     11.69 GiB    567.77 MiB    141.34 MiB      2.21 MiB      8.45 GiB      1.43 GiB           0 B     56.93 MiB     16.38 GiB           0 B           0 B     15.84 GiB
  weka:default2                     21     11.67 GiB    566.70 MiB    141.54 MiB      2.20 MiB      8.44 GiB      1.43 GiB           0 B     56.29 MiB     16.38 GiB           0 B           0 B     15.84 GiB
  weka:default1                     22     11.63 GiB    567.44 MiB    144.39 MiB      2.23 MiB      8.41 GiB      1.39 GiB           0 B     57.26 MiB     16.40 GiB           0 B           0 B     15.84 GiB
  system.slice                      24    592.02 MiB    135.05 MiB     28.09 MiB    888.00 KiB    159.81 MiB    261.23 MiB           0 B    108.44 MiB    181.89 MiB           0 B           0 B           0 B
  user.slice                        16    167.89 MiB     62.84 MiB     39.05 MiB      2.33 MiB     26.00 MiB     91.51 MiB           0 B     32.62 MiB     46.46 MiB           0 B           0 B           0 B
  system                             1     14.00 MiB      6.05 MiB      3.29 MiB     52.00 KiB    176.00 KiB      9.36 MiB           0 B      5.90 MiB      2.18 MiB           0 B           0 B           0 B

HugeTLB (hugetlbfs) users — Private / Shared
  PID       Command                                              Private          Shared           Total
  847602    wekanode --slot 2 --container-name default3        13.01 GiB             0 B       13.01 GiB
  847090    wekanode --slot 2 --container-name default4        13.01 GiB             0 B       13.01 GiB
  848861    wekanode --slot 2 --container-name default2        13.01 GiB             0 B       13.01 GiB
  848842    wekanode --slot 2 --container-name default0        13.01 GiB             0 B       13.01 GiB
  848838    wekanode --slot 2 --container-name default1        13.01 GiB             0 B       13.01 GiB
  847023    wekanode --slot 3 --container-name default4         1.45 GiB             0 B        1.45 GiB
  847593    wekanode --slot 3 --container-name default3         1.45 GiB             0 B        1.45 GiB
  848840    wekanode --slot 3 --container-name default1         1.45 GiB             0 B        1.45 GiB
  848852    wekanode --slot 3 --container-name default2         1.45 GiB             0 B        1.45 GiB
  848879    wekanode --slot 3 --container-name default0         1.45 GiB             0 B        1.45 GiB
  847066    wekanode --slot 1 --container-name default4         1.38 GiB             0 B        1.38 GiB
  847598    wekanode --slot 1 --container-name default3         1.38 GiB             0 B        1.38 GiB
  848839    wekanode --slot 1 --container-name default0         1.38 GiB             0 B        1.38 GiB
  848845    wekanode --slot 1 --container-name default1         1.38 GiB             0 B        1.38 GiB
  848846    wekanode --slot 1 --container-name default2         1.38 GiB             0 B        1.38 GiB

  note: 1 kernel threads / exited processes skipped

Kernel Direct Map (physical memory mapped by page size)
  DirectMap4k:        9.26 GiB
  DirectMap2M:      116.38 GiB
  DirectMap1G:      388.00 GiB
```

## Field reference

See `./memsleuth.py --help-fields` for the canonical, always-in-sync documentation of every column in every table. Highlights:

- Per-process columns come from `/proc/<pid>/smaps` — one pass per process, VMA-by-VMA.
- `ExeSwap` / `FileSwap` use `Size - Rss` on file-backed mappings, which conflates "evicted" with "never faulted in". Large values are common for lazily-loaded fonts or pre-reserved allocator regions; treat them as upper bounds on actual eviction.
- `THP/code` vs `THP/data` split `AnonHugePages + FilePmdMapped` by whether the VMA is executable — i.e. whether transparent huge pages are actually backing your code or only data.
- `HugeTLB` is `Private_Hugetlb + Shared_Hugetlb` — the explicit hugepage pool, separate from THP.

## Tests

A stdlib-only integration suite lives in `tests/`. Each test invokes `memsleuth.py` as a subprocess and asserts on stable substrings in the output, so the tests run on any Linux host without external dependencies and never modify state (destructive flags are exercised via `--dry-run` or via the non-root rejection path).

```bash
python3 -m unittest tests.test_cli            # full suite (~30 s)
python3 -m unittest tests.test_cli.TestDoctor # one class
./tests/test_cli.py -v                        # direct, verbose
```

`memhog.py` is a small helper at the repo root that allocates and touches a configurable fraction of `MemAvailable`, useful for triggering the `--doctor` low-memory alert:

```bash
python3 memhog.py 0.95   # 95% of available; ctrl-c to release
./memsleuth.py --doctor  # in another shell while memhog holds memory
```

## Caveats

- Full `--procs` / `--shared` / `--containers` parse `/proc/<pid>/smaps` for every readable PID, which is noticeably slower than stat-only tools on busy systems. Count on single-digit seconds on a host with thousands of processes.
- Kernel threads have no `smaps` and are skipped; the output reports them separately from real permission-denied failures.
- `~sharers` is a Pss-derived approximation; it treats a VMA as uniformly shared. Mixed-sharing regions (some pages shared, some private) give a blended estimate, not an exact count.
- Container classification is a cgroup-path heuristic; if your runtime uses a layout none of the patterns recognize, classification falls through to `system.slice` / `user.slice` / `system`. Open an issue with a sample `/proc/<pid>/cgroup` and the pattern gets added.
