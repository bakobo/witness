# Decide whether fenced code blocks in docs/ carry language tags, and sweep either way. Raised by Copilot on PR #17 against docs/deploying.md:93 and declined there: that file has 12 fences and all 12 are untagged, so tagging one would make it the odd block out, and no Bakobo standard mentions fences or language tags. The repo is inconsistent BETWEEN files -- README.md uses sh, docs/pools.md mixes 11 bare with 6 tagged -- which is the real question. Same finding and same disposition as bakobo/infra ~67tp; if either repo sweeps, both should.
kind: todo
created: 2026-09-21T21:45Z

