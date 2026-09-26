# Telemetry seqlock has no memory ordering: SegmentWriter and SegmentReader (src/witness/telemetry.py) use plain struct stores/loads on shared mmap, so on a weakly ordered CPU (arm64) the even sequence can become visible before the body stores, and a reader can accept a mixed snapshot (new ticks, old wall). Pre-existing; raised by Codex reviewing PR #21, 2026-09-26. Harmless today because image.yml builds amd64 only (x86 TSO keeps stores ordered), and the fields are advisory telemetry. Becomes real the day an arm64 image ships. Fix options: document the amd64 assumption in @vxt7feoi, or read each field twice / checksum the body. See https://docs.kernel.org/locking/seqlock.html
kind: todo
created: 2026-09-26T09:13Z

