# tests/test_image_upgrade.py leaks its backup volumes. The  fixture removes the container list and the main volume, but the backup/restore test creates a SECOND volume named <name>-backup and nothing removes it, so every local run of that oracle leaves one behind (three were sitting on this box on 2026-09-11: witness-upgrade-*-backup). Harmless individually, a slow accumulation on any machine that runs the oracle often, and CI runners hide it by being ephemeral. Fix: have the fixture sweep by prefix, or label the volumes and remove by label the way witness/pool.py does.
kind: todo
created: 2026-09-11T18:41Z

