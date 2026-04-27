#!/usr/bin/env python3
"""Allocate (and touch) most of available RAM, then sleep until interrupted.

Useful for exercising memsleuth's --doctor low-memory alert.

Usage:
    ./memhog.py [fraction]

`fraction` is the share of MemAvailable to consume (default 0.95). The
script reads /proc/meminfo, allocates a bytearray of that size, and
walks one byte per page so the kernel actually commits the memory
(anonymous allocations are deferred until first write). It then sleeps
forever; press Ctrl-C to release.

The default 0.95 leaves a small safety margin so the OOM killer doesn't
target this process. Drop to 0.85 on tight systems if you've seen
oom-kill activity. Stdlib only, no dependencies.
"""

import os
import re
import sys
import time


def mem_available_bytes() -> int:
    with open("/proc/meminfo") as f:
        for line in f:
            m = re.match(r"MemAvailable:\s+(\d+)\s+kB", line)
            if m:
                return int(m.group(1)) * 1024
    return 0


def main() -> int:
    frac = float(sys.argv[1]) if len(sys.argv) > 1 else 0.95
    if not 0 < frac < 1.0:
        print("fraction must be in (0, 1)", file=sys.stderr)
        return 2
    avail = mem_available_bytes()
    if avail <= 0:
        print("could not read MemAvailable", file=sys.stderr)
        return 1
    size = int(avail * frac)
    gib = 1 << 30
    print("target {:.0%} of {:.2f} GiB available -> allocating {:.2f} GiB"
          .format(frac, avail / gib, size / gib))
    buf = bytearray(size)
    page = os.sysconf("SC_PAGE_SIZE") or 4096
    for i in range(0, size, page):
        buf[i] = 1
    print("resident; PID={}; ctrl-c to release".format(os.getpid()))
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
