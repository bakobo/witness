"""The reference deployment kit in deploy/ (@2hnvwmg2), held to what the image and its edge need.

The kit is configuration a stranger installs by hand, so nothing in this repo executes it and
nothing would notice it drifting. These tests are what notices. Each reads a kit file the way its
consumer would, as far as Python can without the consumer installed, and asserts the property the
file exists to provide:

- compose.yaml runs the image under exactly ``witness.hardening.RUN_FLAGS`` (@4hwwhshr), on
  loopback, by digest.
- The Caddyfile never lets a body reach an upstream that it did not buffer whole, refuses /ext,
  and proxies exactly the change feed and identity of the control plane (@9y806dye), which
  tests/test_deploy_proxy.py then runs through a real Caddy.
- The fail2ban filter's patterns match the Caddy access lines they are meant to and not the ones
  they are not, checked with Python's ``re`` (which is fail2ban's own engine) and, where the
  ``fail2ban-regex`` executable exists, with fail2ban itself.

Consistency with the deployment repository's Ansible is the other half and lives there, because
that repository is private and this one cannot read it.
"""

from __future__ import annotations

import configparser
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from witness.hardening import RUN_FLAGS

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
COMPOSE = DEPLOY / "compose.yaml"
CADDYFILE = DEPLOY / "Caddyfile"
NFTABLES = DEPLOY / "nftables.conf"
FILTER = DEPLOY / "fail2ban" / "filter.d" / "caddy-4xx.conf"
JAIL = DEPLOY / "fail2ban" / "jail.d" / "caddy-4xx.conf"
README = DEPLOY / "README.md"

CONTROL_PORT = "5633"
WITNESS_PORT = "5631"


# --------------------------------------------------------------------------------------------
# compose.yaml
# --------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def service() -> dict:
    services = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]
    assert list(services) == ["witness"], "the kit runs one container; Caddy is a host process"
    return services["witness"]


def compose_flags(svc: dict) -> tuple:
    """The `docker run` flags a compose service's hardening keys amount to, in RUN_FLAGS' order.

    Only the keys @4hwwhshr governs are translated. A key present in one form and absent in the
    other is what the equality below is there to catch.
    """
    flags = []
    if svc.get("read_only") is True:
        flags.append("--read-only")
    for mount in svc.get("tmpfs", []):
        flags += ["--tmpfs", mount]
    for cap in svc.get("cap_drop", []):
        flags += ["--cap-drop", cap]
    for opt in svc.get("security_opt", []):
        flags += ["--security-opt", opt]
    if "pids_limit" in svc:
        flags += ["--pids-limit", str(svc["pids_limit"])]
    return tuple(flags)


def test_compose_runs_the_image_under_exactly_the_hardened_flags(service):
    assert compose_flags(service) == RUN_FLAGS


def test_the_flag_translation_sees_what_it_claims_to():
    """Against a service whose answer is known, so a translator that found nothing could not pass
    the test above vacuously, and an unhardened service visibly differs."""
    assert compose_flags({}) == ()
    assert compose_flags({"read_only": False, "pids_limit": 7}) == ("--pids-limit", "7")


def test_the_image_is_pinned_by_digest_and_never_latest(service):
    image = service["image"]
    assert re.fullmatch(r"ghcr\.io/bakobo/witness@sha256:\S+", image), image
    assert "latest" not in image


def test_both_ports_are_published_to_loopback_only(service):
    ports = service["ports"]
    assert ports, "the witness port must reach Caddy"
    for port in ports:
        assert str(port).startswith("127.0.0.1:"), f"{port} is published beyond loopback"
    published = {str(p).split(":")[1] for p in ports}
    assert published == {WITNESS_PORT, CONTROL_PORT}


def test_the_data_volume_is_mounted_writable_at_the_keri_home(service):
    """@a24p3kbw: a read-only LMDB loses its lock file and its readers lose data silently."""
    mounts = [str(v) for v in service["volumes"]]
    keri = [m for m in mounts if m.split(":")[1] == "/usr/local/var/keri"]
    assert len(keri) == 1, mounts
    assert not keri[0].endswith(":ro")


def test_backups_have_a_writable_destination(service):
    """`witness backup` writes to /backup and nowhere else; under a read-only root, without this
    mount the documented backup command has nowhere to write."""
    mounts = [str(v) for v in service["volumes"]]
    backup = [m for m in mounts if m.split(":")[1] == "/backup"]
    assert len(backup) == 1 and not backup[0].endswith(":ro"), mounts


def test_the_readme_asks_for_a_caddy_that_has_request_buffers():
    """request_buffers arrived in Caddy 2.7; Debian's own package is older and rejects the file,
    so the README must send the reader to Caddy's repository and name the floor."""
    text = README.read_text(encoding="utf-8")
    assert "dl.cloudsmith.io/public/caddy/stable" in text
    assert "2.7" in text
    assert "`apt install caddy` from Debian" not in text


def test_names_match_the_ones_docs_deploying_uses(service):
    """Compose prefixes volume and container names with its project name unless told otherwise, so
    a backup written by the kit would land in a volume the documented restore never mounts."""
    assert service["container_name"] == "witness"
    volumes = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["volumes"]
    assert {key: spec["name"] for key, spec in volumes.items()} == {
        "witness-data": "witness-data", "witness-backups": "witness-backups"}
    deploying = (DEPLOY.parent / "docs" / "deploying.md").read_text(encoding="utf-8")
    for name in ("witness-data:/usr/local/var/keri", "witness-backups:/backup", "--name witness"):
        assert name in deploying


def test_a_memory_limit_is_set(service):
    assert service["mem_limit"] == "512m"


def test_the_operator_allowlist_is_shown_but_not_set(service):
    """No key is invented: an operator must name their own AID, so the variable is a comment."""
    assert "WITNESS_OPERATORS" not in (service.get("environment") or {})
    assert re.search(r"^\s*#\s*-?\s*WITNESS_OPERATORS[=:]", COMPOSE.read_text(), re.M)


# --------------------------------------------------------------------------------------------
# Caddyfile
# --------------------------------------------------------------------------------------------

# go-humanize's table, which is what Caddy parses every size with: KB is a thousand bytes.
_UNITS = {"": 1, "b": 1, "kb": 1000, "mb": 1000**2, "kib": 1024, "mib": 1024**2}


def parse_size(value: str) -> int:
    number, unit = re.fullmatch(r"([0-9]+)([a-zA-Z]*)", value).groups()
    return int(number) * _UNITS[unit.lower()]


def code(text: str) -> str:
    """The text with comments removed, so prose describing a directive cannot satisfy a test."""
    return "\n".join(re.sub(r"#.*", "", line).rstrip() for line in text.splitlines())


def block(text: str, opener: str) -> str:
    """Everything inside the brace block whose opening line matches `opener`, counted by depth."""
    lines = code(text).splitlines()
    start = next(i for i, line in enumerate(lines) if re.search(opener, line))
    depth, out = 1, []
    for line in lines[start + 1:]:
        depth += line.count("{") - line.count("}")
        if depth == 0:
            return "\n".join(out)
        out.append(line)
    raise AssertionError(f"the block opened by {lines[start]!r} never closes")


@pytest.fixture(scope="module")
def caddyfile() -> str:
    return CADDYFILE.read_text(encoding="utf-8")


def site(caddyfile: str) -> str:
    return block(caddyfile, r"^witness\.example\.net\s*\{")


def directive(text: str, name: str) -> str:
    found = re.findall(rf"^\s*{name}\s+(\S+)\s*$", code(text), re.M)
    assert len(found) == 1, f"{name} appears {len(found)} times"
    return found[0]


def proxies(caddyfile: str) -> dict[str, str]:
    """Each reverse_proxy's matcher-and-upstream line, mapped to its block."""
    body = site(caddyfile)
    heads = re.findall(r"^\s*reverse_proxy\s+([^{]+?)\s*\{", body, re.M)
    return {head: block(body, rf"^\s*reverse_proxy\s+{re.escape(head)}\s*\{{") for head in heads}


def test_the_body_cap_equals_every_buffer(caddyfile):
    """The pair's invariant (@pkde7ojw in the deployment): nothing an upstream accepts is ever
    streamed to it, and anything larger is refused. Equal, not merely ordered, for each proxy."""
    cap = parse_size(directive(block(site(caddyfile), r"^\s*request_body\s*\{"), "max_size"))
    buffers = {parse_size(directive(body, "request_buffers")) for body in proxies(caddyfile).values()}
    assert len(proxies(caddyfile)) == 2
    assert buffers == {cap} == {512 * 1000}


def test_request_body_is_a_site_directive_not_a_proxy_one(caddyfile):
    """Caddy rejects request_body inside reverse_proxy at load time."""
    for body in proxies(caddyfile).values():
        assert "request_body" not in body
    assert re.search(r"^\s*request_body\s*\{", site(caddyfile), re.M)


def test_every_timeout_is_thirty_seconds(caddyfile):
    timeouts = block(caddyfile, r"^\s*timeouts\s*\{")
    for name in ("read_body", "read_header", "write"):
        assert directive(timeouts, name) == "30s"
    for body in proxies(caddyfile).values():
        transport = block(body, r"^\s*transport\s+http\s*\{")
        assert directive(transport, "response_header_timeout") == "30s"
        assert directive(transport, "dial_timeout") == "5s"


def test_ext_is_refused_in_both_forms(caddyfile):
    """Caddy's path matcher treats the slash as significant, so /ext/* alone leaves /ext open."""
    body = site(caddyfile)
    matcher = block(body, r"^\s*@refused\s*\{")
    assert set(re.search(r"^\s*path\s+(.+)$", matcher, re.M).group(1).split()) == {"/ext", "/ext/*"}
    assert re.search(r"^\s*respond\s+@refused\s+404\s*$", body, re.M)


WATCHER_ROUTES = {"/v1/witness/change", "/v1/witness/identity"}


def matcher(caddyfile: str, name: str) -> str:
    return block(site(caddyfile), rf"^\s*@{name}\s*\{{")


def test_the_control_plane_is_proxied_only_for_the_watcher_routes(caddyfile):
    """@9y806dye: GET on exactly the feed and identity reaches 5633; everything else goes to the
    witness port, and the control plane's port appears nowhere else."""
    assert list(proxies(caddyfile)) == [f"@watcher 127.0.0.1:{CONTROL_PORT}",
                                        f"127.0.0.1:{WITNESS_PORT}"]
    assert code(caddyfile).count(CONTROL_PORT) == 1
    watcher = matcher(caddyfile, "watcher")
    assert directive(watcher, "method") == "GET"
    assert set(re.search(r"^\s*path\s+(.+)$", watcher, re.M).group(1).split()) == WATCHER_ROUTES


def test_every_other_control_plane_path_is_refused(caddyfile):
    """Both forms, for the slash reason /ext has; the exemption is exactly @watcher's."""
    control = matcher(caddyfile, "control")
    assert set(re.search(r"^\s*path\s+(.+)$", control, re.M).group(1).split()) == {
        "/v1/witness", "/v1/witness/*"}
    exempt = block(control, r"^\s*not\s*\{")
    assert directive(exempt, "method") == "GET"
    assert set(re.search(r"^\s*path\s+(.+)$", exempt, re.M).group(1).split()) == WATCHER_ROUTES
    assert re.search(r"^\s*respond\s+@control\s+404\s*$", site(caddyfile), re.M)


def test_the_access_log_is_json_on_stderr(caddyfile):
    """fail2ban reads it from the journal; a file would need its own ownership and rotation."""
    log = block(site(caddyfile), r"^\s*log\s*\{")
    assert directive(log, "output") == "stderr"
    assert directive(log, "format") == "json"


def test_the_admin_api_is_on_a_unix_socket(caddyfile):
    assert directive(block(caddyfile, r"^\{"), "admin") == "unix//run/caddy/admin.sock"


SECURITY_HEADERS = {
    "Strict-Transport-Security": '"max-age=31536000; includeSubDomains"',
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


def header_fields(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        match = re.match(r"^\s*([A-Z][A-Za-z-]+)\s+(.+?)\s*$", line)
        if match:
            out[match.group(1)] = match.group(2)
    return out


def test_security_headers_on_responses_and_on_errors(caddyfile):
    deferred = block(site(caddyfile), r"^\s*header\s*\{")
    assert re.search(r"^\s*defer\s*$", deferred, re.M), "undeferred, the upstream's own also go out"
    assert re.search(r"^\s*-Server\s*$", deferred, re.M)
    assert header_fields(deferred) == SECURITY_HEADERS
    errors = block(site(caddyfile), r"^\s*handle_errors\s*\{")
    assert header_fields(block(errors, r"^\s*header\s*\{")) == SECURITY_HEADERS
    assert re.search(r"^\s*header\s+-Server\s*$", errors, re.M)


def test_the_block_reader_refuses_an_unclosed_block():
    with pytest.raises(AssertionError, match="never closes"):
        block("site {\n  respond 200\n", r"^site")


# --------------------------------------------------------------------------------------------
# nftables.conf
# --------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def nft() -> str:
    return code(NFTABLES.read_text(encoding="utf-8"))


def test_the_limits_are_per_source_on_input_for_both_families(nft):
    assert re.search(r"type filter hook input priority filter - 10; policy accept;", nft)
    for port in ("80", "443"):
        for family, setname in (("ip", "conn_count"), ("ip6", "conn_count6")):
            assert re.search(
                rf"tcp dport {port} ct state new add @{setname} \{{ {family} saddr ct count over 64 \}} counter drop",
                nft,
            ), (port, family)
        for family, setname in (("ip", "flood_rate"), ("ip6", "flood_rate6")):
            assert re.search(
                rf"tcp dport {port} ct state new add @{setname} \{{ {family} saddr limit rate over "
                rf"25/second burst 60 packets \}} counter drop",
                nft,
            ), (port, family)


def test_ssh_and_loopback_are_accepted_before_any_limit(nft):
    first_limit = nft.index("ct count over")
    assert -1 < nft.index("tcp dport 22 accept") < first_limit
    assert -1 < nft.index("iif lo accept") < first_limit
    assert "dport 22 ct" not in nft, "SSH must never be limited"


def test_the_table_is_replaced_atomically_and_never_the_whole_ruleset(nft):
    """`flush ruleset` would take Docker's chains with it."""
    assert "flush ruleset" not in nft
    preamble = nft.index("table inet witness {}")
    assert preamble < nft.index("delete table inet witness") < nft.index("table inet witness {\n")


# --------------------------------------------------------------------------------------------
# fail2ban
# --------------------------------------------------------------------------------------------


def ini(path: Path) -> configparser.RawConfigParser:
    parser = configparser.RawConfigParser(interpolation=None)
    parser.read_string(path.read_text(encoding="utf-8"))
    return parser


def compiled_filter():
    """The filter's patterns as fail2ban builds them: <ADDR> and the <exempt*> tags substituted.

    fail2ban's own <ADDR> also accepts IPv6 and hostnames; this one is wide enough for the lines
    below, which is all it is asked to be.
    """
    definition = ini(FILTER)["Definition"]
    tags = {k: definition[k] for k in ("exempt", "exempt404")}

    def build(pattern: str) -> re.Pattern:
        for name, value in tags.items():
            pattern = pattern.replace(f"<{name}>", value)
        return re.compile(pattern.replace("<ADDR>", r"(?P<ip>[0-9a-fA-F.:]+)"))

    fail = [build(p) for p in definition["failregex"].strip().splitlines()]
    ignore = [build(p) for p in definition["ignoreregex"].strip().splitlines()]
    return fail, ignore


def caddy_line(ip: str, uri: str, status: int, ua: str = "curl/8") -> str:
    """One Caddy access line, in Caddy's own field order, as `format json` writes it."""
    entry = {
        "level": "info", "ts": 1760000000.0, "logger": "http.log.access.log0",
        "msg": "handled request",
        "request": {"remote_ip": ip, "remote_port": "40000", "client_ip": ip, "proto": "HTTP/2.0",
                    "method": "GET", "host": "witness.example.net", "uri": uri,
                    "headers": {"User-Agent": [ua]}},
        "bytes_read": 0, "user_id": "", "duration": 0.001, "size": 0, "status": status,
        "resp_headers": {"Server": ["Caddy"]},
    }
    return json.dumps(entry, separators=(",", ":"))


def counted(line: str):
    """The address fail2ban would count this line against, or None."""
    fail, ignore = compiled_filter()
    for pattern in fail:
        match = pattern.search(line)
        if match:
            return None if any(p.search(line) for p in ignore) else match.group("ip")
    return None


CASES = [
    (caddy_line("192.0.2.7", "/.env", 404), "192.0.2.7"),
    (caddy_line("2001:db8::7", "/wp-login.php", 403), "2001:db8::7"),
    (caddy_line("192.0.2.8", "/ext/oobi", 404), "192.0.2.8"),
    (caddy_line("192.0.2.9", "/", 413), "192.0.2.9"),
    (caddy_line("192.0.2.10", "/favicon.ico", 413), "192.0.2.10"),
    (caddy_line("198.51.100.1", "/oobi/EAbc", 404), None),
    (caddy_line("198.51.100.2", "/query?typ=logs&pre=EAbc", 400), None),
    (caddy_line("198.51.100.3", "/receipts", 406), None),
    (caddy_line("198.51.100.4", "/favicon.ico", 404), None),
    (caddy_line("198.51.100.5", "/robots.txt", 404), None),
    (caddy_line("198.51.100.6", "/.env", 502), None),
    (caddy_line("198.51.100.7", "/", 200), None),
    (caddy_line("198.51.100.8", "/", 200, ua='x","status":404,"resp_headers":'), None),
]


@pytest.mark.parametrize(("line", "expected"), CASES, ids=[f"case{i}" for i in range(len(CASES))])
def test_the_filter_counts_abuse_and_spares_keri_polling(line, expected):
    assert counted(line) == expected


def test_the_jail_bans_on_the_http_ports_from_the_journal():
    jail = ini(JAIL)["caddy-4xx"]
    assert jail["filter"] == "caddy-4xx"
    assert jail["backend"] == "systemd"
    assert "dport \\{ 80, 443 \\}" in jail["action"], "SSH must be unbannable by port scope"
    assert ini(FILTER)["Definition"]["journalmatch"] == "_SYSTEMD_UNIT=caddy.service"


@pytest.mark.skipif(
    shutil.which("fail2ban-regex") is None,
    reason="fail2ban is not on PyPI and is not a dev dependency; install the distribution's "
    "fail2ban to run the filter through fail2ban itself. The deployment repository's copy of "
    "these files runs there in CI, and its test holds them byte-identical to these.",
)
def test_fail2ban_itself_agrees(tmp_path):
    log = tmp_path / "access.log"
    log.write_text("".join(f"{line}\n" for line, _ in CASES), encoding="utf-8")
    result = subprocess.run(
        ["fail2ban-regex", "--print-all-matched", "--print-no-missed", str(log), str(FILTER)],
        capture_output=True, text=True, check=True, timeout=60,
    )
    expected = sum(1 for _, ip in CASES if ip)
    assert re.search(rf"^Lines: {len(CASES)} lines, \d+ ignored, {expected} matched", result.stdout, re.M), \
        result.stdout


# --------------------------------------------------------------------------------------------
# The kit as a whole
# --------------------------------------------------------------------------------------------

KIT = [COMPOSE, CADDYFILE, NFTABLES, FILTER, JAIL, README]


def test_the_kit_names_no_estate():
    """imbu and any stranger copy this. A vendor, a production hostname or an owner's address in it
    is a fact about one estate presented as a fact about the software."""
    forbidden = re.compile(r"lightsail|\baws\b|amazon|bakobo\.com|wit\.bakobo|\{\{|\{%", re.I)
    for path in KIT:
        hits = forbidden.findall(path.read_text(encoding="utf-8"))
        assert not hits, f"{path.name} names {hits}"


def test_the_readme_carries_the_documented_verify_gate():
    """The cosign gate is copied from docs/deploying.md; the two must not disagree."""
    deploying = (DEPLOY.parent / "docs" / "deploying.md").read_text(encoding="utf-8")
    gate = re.search(r"```\n(cosign verify .*?)```", deploying, re.S).group(1)
    assert " ".join(gate.split()) in " ".join(README.read_text(encoding="utf-8").split())
