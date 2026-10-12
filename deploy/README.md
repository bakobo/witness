# Reference deployment kit

The files here put one witness on the public internet behind the edge it needs. The image alone is not enough: keripy's HTTP server is single-threaded, its OOBI-resolution route will fetch any URL a stranger posts, and it has no notion of a slow, oversized or abusive client. Everything in this directory exists to keep those callers away from it.

The kit is a reference, not an installer. It assumes a Debian-family host with systemd, Docker with the compose plugin, and Caddy, nftables and fail2ban from the distribution. It names no vendor and no estate. Replace `witness.example.net`, `operator@example.net` and `sha256:<digest>`; nothing else is a placeholder. [`docs/deploying.md`](../docs/deploying.md) is the contract for the image itself and is linked rather than repeated below. `tests/test_deploy_kit.py` holds these files to the image's hardening and to each other.

| File | Install as |
| --- | --- |
| `compose.yaml` | anywhere; run `docker compose` beside it |
| `Caddyfile` | `/etc/caddy/Caddyfile` |
| `nftables.conf` | `/etc/nftables.d/witness.nft` |
| `fail2ban/filter.d/caddy-4xx.conf` | `/etc/fail2ban/filter.d/caddy-4xx.conf` |
| `fail2ban/jail.d/caddy-4xx.conf` | `/etc/fail2ban/jail.d/caddy-4xx.conf` |

## What each piece defends against, and what it does not

**`compose.yaml`: the container.** It runs the image under the hardened runtime flags ([`@4hwwhshr`](../docs/deploying.md#what-to-run)): read-only root filesystem, every capability dropped, no setuid route back to one, a bounded process table, and `/tmp` as a small noexec tmpfs. A compromised witness process can write only its data volume. Both ports are published to `127.0.0.1` only, and the memory limit is sized for the LMDB database rather than the interpreter ([sizing](../docs/deploying.md#three-things-that-are-silent-when-wrong)). It does not protect the data volume, which holds the witness's unencrypted signing key: the host's uid boundary does that.

**`Caddyfile`: the edge.** TLS by ACME. Thirty-second read, write and upstream-header timeouts, so a slow client costs Caddy and not the witness. The whole request is buffered before the witness sees it, and any body over the same 512KB is refused with a 413, so nothing is ever streamed into the witness's loop. `/ext`, keripy's unauthenticated OOBI-resolution route, is answered 404 and never proxied, which stops a stranger making the witness fetch arbitrary URLs. HSTS and the usual browser headers go on every response, error responses included. The admin API is on a unix socket instead of `localhost:2019`. The access log goes to the journal as JSON, which is what fail2ban reads. Caddy never proxies the control plane. It does not rate-limit requests: that needs a third-party plugin, and the kit leaves request volume to the two pieces below.

**`nftables.conf`: connection floods.** Per source address, on ports 80 and 443, for IPv4 and IPv6: at most 64 concurrent connections and 25 new connections a second after a burst of 60. Excess SYNs are dropped in the kernel before Caddy sees them. **It bounds connections, not requests**, and nothing at this layer can: one HTTP/2 connection carrying ten thousand requests passes untouched. It covers TCP only, so HTTP/3 over UDP 443 is not limited here. It hooks `input` because Caddy, a host process, terminates public traffic there; it would be inert in `DOCKER-USER` ([why](../docs/deploying.md#three-things-that-are-silent-when-wrong)). SSH and loopback are accepted before any limit and are never counted.

**`fail2ban/`: sources that keep failing.** Ten counted 4xx responses from one TCP peer within ten minutes bans that peer for an hour on TCP and UDP 80 and 443, doubling per repeat up to a day. KERI's `/oobi`, `/receipts` and `/query` are exempt, because a controller polling for something the witness does not hold yet gets exactly those 4xx, and so are 404s on `/favicon.ico` and `/robots.txt`. 5xx never counts. **fail2ban needs Caddy as the TCP edge**: it bans the address Caddy logs as `remote_ip`, which is the packet's source only while nothing sits in front of Caddy. Put a load balancer or CDN in front and every ban lands on the proxy's address. The witness never reads `X-Forwarded-For`, and neither does this filter. The ban is port-scoped, so SSH cannot be banned for anyone. It does not see abuse that succeeds: a flood of 200s is nftables' and the witness's problem.

**The control plane is not exposed at all.** Port 5633 stays on loopback. Reach it over SSH and sign each request with `witness get` ([authentication](../README.md#authentication)):

```
ssh -N -L 5633:127.0.0.1:5633 <you>@witness.example.net &
witness get --key ~/.witness-operator http://127.0.0.1:5633/v1/witness/health
```

Six of its nouns answer only operators named in `WITNESS_OPERATORS`; set it in `compose.yaml` with your own AID. Unset, those six refuse everyone and health and identity still answer.

## Install, in this order

The order matters in two places. Installing the `nftables` package on a host where Docker is already running can flush Docker's rules, so nftables goes first. And Caddy must be logging to the journal before fail2ban starts reading it.

1. **nftables, before Docker.** `apt install nftables`, copy `nftables.conf` to `/etc/nftables.d/witness.nft`, append `include "/etc/nftables.d/*.nft"` to `/etc/nftables.conf` so boot loads it, `systemctl enable nftables`, then apply it now with `nft -f /etc/nftables.d/witness.nft`. Never `systemctl reload nftables` on a Docker host: it runs `flush ruleset` and takes Docker's chains with it. If that happens, `systemctl restart docker` restores them. If Docker was already installed, run `systemctl restart docker` after installing the package anyway.
2. **Docker and the witness.** Verify the digest first; refuse to start it if this fails ([why these two pins](../docs/deploying.md#verify-before-you-run-a-digest)):

   ```
   cosign verify ghcr.io/bakobo/witness@sha256:<digest> \
       --certificate-oidc-issuer https://token.actions.githubusercontent.com \
       --certificate-identity-regexp '^https://github\.com/bakobo/witness/\.github/workflows/image\.yml@refs/tags/v[0-9]+\.[0-9]+\.[0-9]+$'
   ```

   Then put the digest in `compose.yaml`, create the keystore, and start it:

   ```
   docker compose run --rm --entrypoint kli witness init --name witness --nopasscode
   docker compose up -d
   ```
3. **Caddy.** `apt install caddy`. Give it a runtime directory for the admin socket with a drop-in at `/etc/systemd/system/caddy.service.d/admin-socket.conf` holding `[Service]`, `RuntimeDirectory=caddy` and `RuntimeDirectoryMode=0750`, then `systemctl daemon-reload`. Copy the `Caddyfile`, check it with `caddy validate --config /etc/caddy/Caddyfile`, and `systemctl restart caddy`. The first switch to the socket must be a restart, because a reload posts to the admin address the running config names, which is still 2019. Later changes can be reloads. Point DNS at the host before this step, or ACME cannot issue.
4. **fail2ban, last.** `apt install fail2ban`, copy both files from `fail2ban/`, and add a `jail.d/*.local` of your own for anything host-specific, such as `ignoreip` for your own addresses. Debian enables an sshd jail by default; decide whether you want it. Then `systemctl restart fail2ban`.

The [backup, restore and upgrade procedures](../docs/deploying.md#upgrading) apply unchanged; use `docker compose exec witness witness backup ...` in place of `docker exec`.

## Verify each protection is live

Configured is not the same as live. Each check below fails visibly if its piece is not doing its job.

**The container.** `docker compose exec witness sh -c 'grep -E "CapEff|NoNewPrivs" /proc/self/status; touch /usr/x'` should print `CapEff: 0000000000000000` and `NoNewPrivs: 1`, then refuse the write as a read-only file system. `ss -ltn | grep -E ':563[13]'` should show both ports on `127.0.0.1` only.

**The edge.** From another machine:

```
curl -sI https://witness.example.net/ | grep -iE 'strict|nosniff|referrer|frame|server'
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://witness.example.net/ext/oobi -d '{}'
head -c 600000 /dev/zero | curl -s -o /dev/null -w '%{http_code}\n' -X POST --data-binary @- https://witness.example.net/
```

Expect the four headers and no `Server`, then `404`, then `413`. On the host, `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:2019/config/` should print `000` (refused), and `ls -l /run/caddy` should show `admin.sock` owned by `caddy`.

**nftables.** `nft list table inet witness | grep -E 'ct count|limit rate'` shows the rules in the kernel with their counters. To trip them, from a workstation whose address you can afford to lose ports 80 and 443 for a minute (SSH is never limited):

```
seq 1 300 | xargs -P 150 -I{} curl -s -o /dev/null -m 5 -w '%{http_code}\n' https://witness.example.net/oobi | sort | uniq -c
python3 -c "import socket;s=[socket.create_connection(('witness.example.net',443),timeout=3) for _ in range(64)];print('held 64');socket.create_connection(('witness.example.net',443),timeout=3)"
```

The first should show a band of `000` timeouts among the responses, and the `@flood_rate` counters should rise. The second should print `held 64` and then time out on connection 65, and the `@conn_count` counter should rise. Afterwards the witness should answer again within a minute.

**fail2ban.** `fail2ban-client status caddy-4xx` should list the jail with `Journal matches: _SYSTEMD_UNIT=caddy.service`. `fail2ban-regex systemd-journal /etc/fail2ban/filter.d/caddy-4xx.conf` runs the filter over the journal you already have and reports how many lines it would count. To see a ban, request eleven nonexistent paths from a disposable address and watch `fail2ban-client status caddy-4xx` list it, then `nft list table inet f2b-table` show it. `fail2ban-client set caddy-4xx unbanip <address>` lifts it.
