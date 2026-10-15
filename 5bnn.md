# tests/test_deploy_kit.py: require /ext refusal and body cap at the site block's top level, and the ruleset to hold only the input chain and one table
kind: todo
created: 2026-10-12T23:39Z

- 2026-10-12T23:39Z Raised by the hostile pass on bakobo/infra#107 against infra's consistency test; witness's own tests/test_deploy_kit.py has the same shape (searches the site block anywhere, and reads the input chain only). infra#107 now catches all four mutations against the kit at witness main, so the kit is guarded; this is for the public repo to guard itself.
- 2026-10-15T00:52Z Landed on utah/next as cd6a5b9 (2026-10-15, via pr-merge land); done once utah/next reaches main.
