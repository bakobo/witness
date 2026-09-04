# Prove the LMDB upgrade path across keripy pins (OPS-F5). Bringing a new image up on an older image's volume without re-incepting is undocumented and untested, and whether kli migrate run is ever needed between pins is unknown. It is the operation that will matter most the first time it is needed and the worst one to discover live. Wants a test that starts image A, writes state, then starts image B on the same volume and asserts the AID, KEL and receipts survive and a new event is still accepted.
kind: todo
created: 2026-09-04T02:05Z

