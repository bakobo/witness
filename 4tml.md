# Decide whether tag taint propagates through delegation. Today tags.derive() looks only at an AID's OWN witness list (state.b), so a delegated AID whose DELEGATOR is witnessed by a testnet witness inherits nothing. Arguably wrong: a delegated identifier's authority is rooted in its delegator, so laboratory infrastructure under the delegator taints everything below it. Left undecided in @vqqh6zdk deliberately -- the control plane cannot see the delegator's key state reliably (state.di names the delegator, but this witness may hold no key state for it), so the honest implementation is probably to report the delegator in 'unresolved' rather than to walk it. Needs an intent node either way.
kind: todo
created: 2026-09-16T18:32Z

