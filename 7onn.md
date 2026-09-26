# Registrar admission-DNS cap has two availability gaps (Codex on witness#19 at 129f2fd): _admit (app.py:165) releases its reservation when it tracks a worker that _within (batcher.py:216) starts only afterwards, so a concurrent prune can drop the unstarted thread and let the 16-resolution cap be exceeded; and 16 resolutions that never return hold admission at 503 until restart, since the 5 s deadline stops waiting but cannot kill getaddrinfo, and admission runs before replay recording so repeats of one signed request can occupy slots. Fix: count a thread from reservation until it ends, check replay before admission, and resolve through a bounded async resolver or a subprocess that can be killed
kind: debt
tags: registrar, availability
created: 2026-09-25T03:57Z

- 2026-09-26T09:01Z PR bakobo/witness#22 (2026-09-26) fixes all three gaps; tick off when it merges.
