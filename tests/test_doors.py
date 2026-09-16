"""The door census (@bb3yndtf), required by dev/standards/input-handling.md.

The standard asks every repo that handles input to carry one test enumerating the primitives that
bring bytes across a boundary — file opens, HTTP calls, argv, environment reads, standard input —
and to assert that each call site sits inside a named door or appears below as an exemption with a
written reason.

The rule is not the point. Completeness is, and completeness decays: a new call site that reads a
file directly does not look wrong when you write it, and prose cannot make the set of doors
visible the way a failing test can. So this is a test rather than a section in a document, and it
fails in both directions — an unlisted call site fails it, and a listed one that has gone away
fails it too, so the inventory cannot quietly describe a repo that no longer exists.

An exemption is not a weakness here. It is where somebody had to write, in prose, why a particular
read needs no door, and that sentence is the artifact a reviewer can disagree with.
"""

import ast
import datetime
import pathlib

import pytest


#: When a person last read every entry below and confirmed it still says something true.
#:
#: The census catches a NEW call site by itself. What it cannot catch is an exemption whose reason
#: quietly stopped being true — the code around it changed, the bound moved, the threat it dismissed
#: became real. Only a person re-reading them catches that, so the date records when one last did.
#: Bump it when you have actually re-read the reasons, not when you add an entry.
LAST_REVIEWED = "2026-09-16"

SOURCE = pathlib.Path(__file__).resolve().parent.parent / "src" / "witness"

#: Bare-name calls that cross a boundary wherever they appear.
_BARE = {"open", "input"}

#: Module-qualified calls that cross a boundary. Keyed by the root module name so that
#: ``subprocess.run`` is caught and a ``run()`` method of ours is not — an earlier draft of this
#: scan flagged three of our own ``.run()`` calls and one dataclass field named ``argv``.
_QUALIFIED = {
    ("subprocess", "run"),
    ("subprocess", "Popen"),
    ("subprocess", "check_output"),
    ("urllib", "urlopen"),
    ("request", "urlopen"),
    ("os", "open"),
    ("os", "getenv"),
}

#: Method names that cross a boundary whichever receiver they are called on, because the receiver
#: is usually an instance rather than a module: ``Path(p).read_text()`` and ``handle.read_text()``
#: both land here. A first draft listed these under _QUALIFIED as ("pathlib", "read_text"), which
#: can never match — nobody calls read_text on the module — so the entries implied a coverage the
#: scan did not have. Matching on the name over-flags rather than under-flags, and that asymmetry
#: is the right one for a census: an over-flag costs somebody one line of accounting, where an
#: under-flag is a boundary nobody knows about.
_METHODS = {"read_text", "read_bytes"}

#: Attribute reads that are themselves a boundary.
_ATTRIBUTES = {("os", "environ"), ("sys", "argv"), ("sys", "stdin")}

#: Every boundary crossing in src/witness, and what accounts for it.
#:
#: Keyed by ``module.py::enclosing.function``. A value beginning "door:" names the door the read
#: sits inside; anything else is an exemption and has to say why no door is needed.
ACCOUNTED = {
    "config.py::_add_pool_parser": (
        "door: config.parse_args. WITNESS_IMAGE is read as an argparse default, so it enters "
        "through the same argument door as everything else and is bounded by the same parser."
    ),
    "metrics.py::configured_endpoint": (
        "exempt: the OTLP endpoint is read once at startup and handed to the OpenTelemetry SDK, "
        "which owns its validation. Bounding it here would mean this repo forming an opinion "
        "about a URL shape the SDK already decides, and disagreeing with it silently."
    ),
    "reader.py::WitnessReader._seeded": (
        "door: decls.from_seed. The read is bounded to MAX_SEED_BYTES before parsing, and every "
        "failure — absent, unreadable, not UTF-8, malformed, refused by the vocabulary — returns "
        "no declarations rather than propagating."
    ),
    "pool.py::run_docker": (
        "exempt: docker's stdout and stderr are read from a subprocess this repo spawned with a "
        "fixed binary and arguments it built itself, under a timeout. The bytes are parsed by "
        "format-specific callers rather than trusted here, and the blast radius is a laboratory "
        "tool that runs on an operator's own host."
    ),
    "pool.py::fetch_json": (
        "exempt: a loopback GET to a control plane in a pool this repo just created, decoded "
        "inside a try that turns any failure into None. Unreachable is an ordinary state for a "
        "pool, so the caller decides what it means. Not a door because nothing downstream trusts "
        "the value: it is displayed or compared, never used as authority."
    ),
    "backup.py::back_up": (
        "exempt: output, not input. This opens the manifest for writing; nothing is read."
    ),
    "telemetry.py::SegmentWriter.create": (
        "exempt: output, not input. The witness's own loop writes its telemetry segment here."
    ),
    "telemetry.py::SegmentReader.__init__": (
        "door: telemetry.SegmentReader. The segment is mapped read-only and every read is a "
        "fixed-width struct at a fixed offset, so its size is the bound and a short or corrupt "
        "segment raises TelemetryUnavailable rather than yielding a value."
    ),
    "vitals.py::_cmdline": (
        "door: vitals. A /proc read for a pid this process just resolved. The kernel bounds the "
        "content, OSError means the process exited between listing and reading, and the value is "
        "reported rather than acted on."
    ),
    "vitals.py::runner_vitals": (
        "door: vitals. As above, for /proc/<pid>/stat and /proc/<pid>/status. Parsed positionally "
        "against the kernel's documented format, with a typed error when it does not match."
    ),
}


def _crossings(source=None):
    """Every boundary crossing the scan can find, as ``module.py::enclosing.function``."""
    found = {}
    for path in sorted((source or SOURCE).rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        stack = []

        def root_of(node):
            while isinstance(node, ast.Attribute):
                node = node.value
            return getattr(node, "id", None)

        def record(lineno):
            where = f"{path.name}::{'.'.join(stack) or '<module>'}"
            found.setdefault(where, []).append(lineno)

        class Walker(ast.NodeVisitor):
            def visit_FunctionDef(self, node):
                stack.append(node.name)
                self.generic_visit(node)
                stack.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_ClassDef(self, node):
                stack.append(node.name)
                self.generic_visit(node)
                stack.pop()

            def visit_Call(self, node):
                func = node.func
                if isinstance(func, ast.Name) and func.id in _BARE:
                    record(node.lineno)
                elif isinstance(func, ast.Attribute):
                    if (
                        func.attr in _METHODS
                        or (root_of(func.value), func.attr) in _QUALIFIED
                    ):
                        record(node.lineno)
                self.generic_visit(node)

            def visit_Attribute(self, node):
                if (root_of(node.value), node.attr) in _ATTRIBUTES:
                    record(node.lineno)
                self.generic_visit(node)

        Walker().visit(tree)
    return found


def test_every_boundary_crossing_is_accounted_for():
    """A read that entered through no door and claimed no exemption fails here."""
    crossings = _crossings()
    unaccounted = {
        where: lines for where, lines in crossings.items() if where not in ACCOUNTED
    }
    assert not unaccounted, (
        "these read from a boundary and are neither inside a named door nor exempted with a "
        f"reason: {unaccounted}. Add them to ACCOUNTED in this file, and if the answer is an "
        "exemption, write the sentence saying why no door is needed."
    )


def test_the_inventory_describes_a_repo_that_still_exists():
    """The other direction, so the list cannot rot into a description of code that is gone."""
    crossings = _crossings()
    stale = sorted(set(ACCOUNTED) - set(crossings))
    assert not stale, (
        f"these entries no longer match any boundary crossing: {stale}. Remove them, or find out "
        "what moved."
    )


@pytest.mark.parametrize("where", sorted(ACCOUNTED))
def test_every_entry_is_a_door_or_an_exemption(where):
    """Each entry has to commit to one or the other, so neither can be implied by silence.

    Deliberately no minimum length on the reason. A first draft required one and the shortest
    reason here — that a call writes rather than reads — tripped it while being complete. A length
    bar cannot tell a padded sentence from a substantial one, so it only teaches people to pad;
    whether a reason actually holds is what LAST_REVIEWED exists to record a person checking.
    """
    reason = ACCOUNTED[where]
    assert reason.startswith(("door:", "exempt:")), (
        f"{where} is neither a door nor an exemption, so nobody has said which it is"
    )


def test_the_review_date_is_a_real_date_that_has_happened():
    """A typo'd or aspirational date would make "last reviewed" mean nothing at all."""
    reviewed = datetime.date.fromisoformat(LAST_REVIEWED)
    assert reviewed <= datetime.date.today(), "the census cannot have been reviewed in the future"


class TestTheScanItself:
    """A census that silently found nothing would pass too, so prove the gate bites.

    Each case plants source in a temporary tree and scans that instead of src/witness.
    """

    def _scan(self, tmp_path, source):
        (tmp_path / "planted.py").write_text(source, encoding="utf-8")
        return _crossings(tmp_path)

    def test_a_plain_file_read_is_found(self, tmp_path):
        found = self._scan(tmp_path, "def loader():\n    return open('/etc/passwd').read()\n")
        assert "planted.py::loader" in found

    def test_a_subprocess_is_found(self, tmp_path):
        found = self._scan(
            tmp_path, "import subprocess\ndef shell():\n    return subprocess.run(['ls'])\n"
        )
        assert "planted.py::shell" in found

    def test_an_environment_read_is_found(self, tmp_path):
        found = self._scan(tmp_path, "import os\ndef conf():\n    return os.environ.get('X')\n")
        assert "planted.py::conf" in found

    def test_a_read_inside_a_method_is_attributed_to_the_class(self, tmp_path):
        found = self._scan(
            tmp_path, "class Loader:\n    def load(self):\n        return open('x').read()\n"
        )
        assert "planted.py::Loader.load" in found

    def test_our_own_run_method_is_not_mistaken_for_a_subprocess(self, tmp_path):
        """The false positive an earlier draft had: three of our own .run() calls and a dataclass
        field named argv, none of which cross anything."""
        found = self._scan(
            tmp_path,
            "class Spec:\n    argv = ()\n\n"
            "def main(pool, spec):\n    spec.argv\n    return pool.run()\n",
        )
        assert found == {}

    def test_a_path_read_is_found_on_a_constructed_object(self, tmp_path):
        """The hole a first draft had: read_text on an instance, not on the module."""
        found = self._scan(
            tmp_path,
            "from pathlib import Path\ndef load():\n    return Path('/etc/x').read_text()\n",
        )
        assert "planted.py::load" in found

    def test_a_path_read_is_found_on_a_bound_variable(self, tmp_path):
        found = self._scan(
            tmp_path, "def load(where):\n    return where.read_bytes()\n"
        )
        assert "planted.py::load" in found

    def test_an_unaccounted_crossing_is_what_fails_the_census(self, tmp_path):
        """The census asserts membership in ACCOUNTED, so this shows the assertion it makes."""
        found = self._scan(tmp_path, "def sneaky():\n    return open('x').read()\n")
        assert set(found) - set(ACCOUNTED) == {"planted.py::sneaky"}
