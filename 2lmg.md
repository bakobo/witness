# GET /info accepts a non-witness keystore: _select_witness_hab only rejects GROUP habs (mid is not None), never transferable ones. Pointed at a controller's keystore it reports that controller's transferable AID as the witness identity (demonstrated 2026-08-06). A witness AID is always non-transferable — a transferable one would need witnesses of its own. Guard with Prefixer(qb64=hr.hid).transferable. Also splits IdentityUnavailable in two: no hab at all (pending, retryable) vs habs present but none is a witness (misconfiguration, final).
kind: todo
created: 2026-08-06T23:33Z

