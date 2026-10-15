"""The runtime flag set every `docker run` of the image passes (@4hwwhshr).

The flags live once, in ``witness.hardening.RUN_FLAGS``. The oracles start containers with them,
the pool starts containers with them, and the deployment documentation tells an operator to use
them. The last of those is prose, and prose drifts silently, so this file holds the docs to the
constant: every ``docker run`` an operator is told to type must carry the same set, spelled the
same way.
"""

import re
import shlex
from pathlib import Path

import pytest

from witness.hardening import RUN_FLAGS

ROOT = Path(__file__).resolve().parent.parent
DEPLOYING = ROOT / "docs" / "deploying.md"
#: Every document that tells an operator to type `docker run`, each held to the constant.
RUN_DOCS = [DEPLOYING, ROOT / "README.md"]


def _pairs(flags):
    """Flag -> value, for the flags that take one, so assertions read as properties."""
    out, it = {}, iter(flags)
    for flag in it:
        out[flag] = True if flag == "--read-only" else next(it)
    return out


def test_the_root_filesystem_is_read_only_and_only_tmp_is_scratch():
    flags = _pairs(RUN_FLAGS)
    assert flags["--read-only"] is True
    tmpfs = flags["--tmpfs"]
    path, _, options = tmpfs.partition(":")
    assert path == "/tmp", "hio resolves a temp dir at import; /tmp is the one scratch path"
    assert {"noexec", "nosuid", "nodev"} <= set(options.split(","))
    assert any(option.startswith("size=") for option in options.split(",")), "bounded"


def test_no_capabilities_no_new_privileges_and_a_bounded_process_table():
    flags = _pairs(RUN_FLAGS)
    assert flags["--cap-drop"] == "ALL"
    assert flags["--security-opt"] == "no-new-privileges"
    assert int(flags["--pids-limit"]) == 128


def test_the_data_volume_is_never_made_read_only_by_the_flag_set():
    """@a24p3kbw: a read-only LMDB loses its lock file and its readers lose data silently."""
    joined = " ".join(RUN_FLAGS)
    assert "/usr/local/var/keri" not in joined
    assert ":ro" not in joined


def _commands(markdown):
    """Every shell command in a fenced block, with backslash continuations joined."""
    commands = []
    for block in re.findall(r"^```[^\n]*\n(.*?)^```", markdown, flags=re.M | re.S):
        joined = re.sub(r"\s*\\\n\s*", " ", block)
        commands.extend(line.strip() for line in joined.splitlines() if line.strip())
    return commands


def _documented_flags(markdown):
    """The value the docs assign to HARDENED, split as the shell would split `$HARDENED`."""
    found = [c for c in _commands(markdown) if c.startswith("HARDENED=")]
    assert len(found) == 1, "a document that runs the image should define HARDENED exactly once"
    return tuple(shlex.split(shlex.split(found[0])[0].partition("=")[2]))


@pytest.mark.parametrize("doc", RUN_DOCS, ids=lambda p: p.name)
def test_the_documented_flag_set_is_the_constant(doc):
    assert _documented_flags(doc.read_text(encoding="utf-8")) == RUN_FLAGS


@pytest.mark.parametrize("doc", RUN_DOCS, ids=lambda p: p.name)
def test_every_documented_docker_run_carries_the_flag_set(doc):
    runs = [c for c in _commands(doc.read_text(encoding="utf-8")) if "docker run" in c]
    assert runs, f"{doc.name} should show how to run the image"
    unhardened = [c for c in runs if "$HARDENED" not in c]
    assert not unhardened, f"these documented runs omit $HARDENED: {unhardened}"


def test_the_doc_checks_see_what_they_claim_to():
    """The two helpers above against a document whose answer is known, so a parser that quietly
    found nothing could not make the docs tests pass vacuously."""
    doc = (
        "prose docker run outside a fence\n"
        "```\n"
        'HARDENED="--read-only --pids-limit 1"\n'
        "docker run -d $HARDENED \\\n"
        "    image\n"
        "```\n"
        "```sh\n"
        "docker run --rm image\n"
        "```\n"
    )
    assert _documented_flags(doc) == ("--read-only", "--pids-limit", "1")
    assert _commands(doc) == [
        'HARDENED="--read-only --pids-limit 1"',
        "docker run -d $HARDENED image",
        "docker run --rm image",
    ]
