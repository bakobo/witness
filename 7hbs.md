# OPS-F1 + OPS-F2 (2026-09-26 health panel): the release procedure is undocumented -- version bump, tag, and the post-publish update of the DEPLOYED_WITNESS_IMAGE repo variable live only in image.yml comments and bakobo/infra's bin/release-witness. And nothing alarms when DEPLOYED_WITNESS_IMAGE is unset or stale, so the two-version upgrade oracle silently skips or tests the wrong baseline (image.yml:99-118). Write the release section (CONTRIBUTING.md or docs/deploying.md) from what bin/release-witness actually does, and consider having the image workflow compare the variable against the digest infra pins.
kind: todo
created: 2026-09-26T09:21Z

