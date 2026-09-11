"""Pool tests: the laboratory verb (@n2bgpdds), with Docker and HTTP both injected.

Every docker invocation and every control-plane request is a seam, so these tests never start a
container. What is being pinned here is the *policy* — the order the steps happen in, what carries
a label, what the manifest says, and what each failure does — and a test that had to run docker to
prove any of it would be slower and would prove less. The end-to-end proof that the policy actually
stands up witnesses lives in ``tests/test_pool_oracle.py``, which runs the real image.

The ordering assertions are the load-bearing ones. A config seeded after the witness has started
is a config keripy never reads (`makeHab` consults it once, when the hab is created), so
"init, then seed, then run" is a property of this module and not an implementation detail.
"""

import json
import urllib.error

import pytest

from witness.config import PoolConfig
from witness.errors import (
    DockerUnavailable,
    InvalidArguments,
    PoolExists,
    PoolNotReady,
    PoolUnknown,
    WitnessUnknown,
)
from witness.pool import Pool

# What `docker ps -a --format ...` returns for a healthy two-witness pool, in docker's own order
# (newest first), which is deliberately NOT the pool's order.
_TWO_UP = (
    "witness-pool-lab-w2\tw2\t5650\t5652\trunning\twitness:test\n"
    "witness-pool-lab-w1\tw1\t5640\t5642\trunning\twitness:test\n"
)


class FakeDocker:
    """Records every invocation and answers from a substring-keyed script."""

    def __init__(self, script=None, fails=()):
        self.calls = []
        self._script = script or {}
        self._fails = set(fails)

    def __call__(self, args, *, check=True, timeout=None):
        joined = " ".join(args)
        self.calls.append(joined)
        for fragment in self._fails:
            if fragment in joined:
                raise DockerUnavailable(f"Docker refused: {fragment}.")
        for fragment, answer in self._script.items():
            if fragment in joined:
                return answer
        return ""

    def matching(self, fragment):
        return [call for call in self.calls if fragment in call]


class FakeFetch:
    """Answers control-plane requests from a substring-keyed script; unknown URLs are unreachable."""

    def __init__(self, script=None):
        self.calls = []
        self._script = script or {}

    def __call__(self, url, *, timeout=None):
        self.calls.append(url)
        for fragment, answer in self._script.items():
            if fragment in url:
                return answer(self) if callable(answer) else answer
        return None


def _healthy(ports=(5640, 5650), aids=("BAAA", "BBBB")):
    script = {}
    for index, port in enumerate(ports):
        control = port + 2
        script[f":{control}/v1/witness/health"] = {"status": "ok", "ticks": 42}
        script[f":{control}/v1/witness/identity"] = {"aid": aids[index], "alias": "witness"}
        script[f":{control}/v1/witness/controller"] = {"controllers": [{"aid": "EDDD"}]}
    return script


def _pool(verb, docker=None, fetch=None, **kwargs):
    kwargs.setdefault("image", "witness:test")
    kwargs.setdefault("name", "lab")
    config = PoolConfig(verb=verb, **kwargs)
    written = []
    return (
        Pool(
            config,
            docker=docker if docker is not None else FakeDocker(),
            fetch=fetch if fetch is not None else FakeFetch(),
            sleep=lambda _seconds: None,
            clock=iter(range(0, 10000)).__next__,
            now=lambda: "2026-09-11T00:00:00.000000+00:00",
            out=written.append,
        ),
        written,
    )


def _printed(written):
    return "".join(written)


class TestUp:
    def test_it_initialises_seeds_and_starts_each_witness_in_that_order(self):
        docker = FakeDocker(script={"ps -a": ""})
        fetch = FakeFetch(_healthy())
        pool, written = _pool("up", docker=docker, fetch=fetch, count=2)

        assert pool.run() == 0

        order = [call for call in docker.calls if "volume create" in call or "kli" in call
                 or "configing" in call or "run -d" in call]
        assert ["volume create" in order[0], "kli" in order[1]] == [True, True]
        assert "configing" in order[2], "the config must be seeded before the witness starts"
        assert "run -d" in order[3]
        assert len(order) == 8, "two witnesses, four steps each"

    def test_every_container_and_volume_carries_the_pool_label(self):
        docker = FakeDocker(script={"ps -a": ""})
        pool, _ = _pool("up", docker=docker, fetch=FakeFetch(_healthy()), count=2)
        pool.run()

        for call in docker.matching("volume create") + docker.matching("run -d"):
            assert "--label bakobo.pool=lab" in call
        assert "--label bakobo.pool.witness=w2" in docker.matching("run -d")[1]

    def test_ports_step_by_ten_from_the_base_and_bind_to_loopback(self):
        docker = FakeDocker(script={"ps -a": ""})
        pool, _ = _pool("up", docker=docker, fetch=FakeFetch(_healthy()), count=2)
        pool.run()

        first, second = docker.matching("run -d")
        assert "-p 127.0.0.1:5640:5631" in first and "-p 127.0.0.1:5642:5633" in first
        assert "-p 127.0.0.1:5650:5631" in second and "-p 127.0.0.1:5652:5633" in second

    def test_the_seeded_config_advertises_the_published_url_under_the_alias(self):
        docker = FakeDocker(script={"ps -a": ""})
        pool, _ = _pool("up", docker=docker, fetch=FakeFetch(_healthy()), count=1)
        pool.run()

        seeded = docker.matching("configing")[0]
        payload = json.loads(seeded.rsplit(" ", 1)[-1])
        assert payload == {
            "witness": {
                "dt": "2026-09-11T00:00:00.000000+00:00",
                "curls": ["http://127.0.0.1:5640/"],
            }
        }

    def test_a_seed_makes_the_keys_reproducible_and_its_absence_leaves_them_random(self):
        seeded = FakeDocker(script={"ps -a": ""})
        pool, _ = _pool("up", docker=seeded, fetch=FakeFetch(_healthy()), count=1, seed="merti")
        pool.run()
        again = FakeDocker(script={"ps -a": ""})
        pool, _ = _pool("up", docker=again, fetch=FakeFetch(_healthy()), count=1, seed="merti")
        pool.run()
        unseeded = FakeDocker(script={"ps -a": ""})
        pool, _ = _pool("up", docker=unseeded, fetch=FakeFetch(_healthy()), count=1)
        pool.run()

        assert seeded.matching("kli")[0] == again.matching("kli")[0]
        assert "--salt" in seeded.matching("kli")[0]
        assert "--salt" not in unseeded.matching("kli")[0]

    def test_it_reports_the_aid_and_urls_it_ended_up_with(self):
        pool, written = _pool("up", docker=FakeDocker(script={"ps -a": ""}),
                              fetch=FakeFetch(_healthy()), count=2)
        pool.run()

        report = _printed(written)
        assert "BAAA" in report and "BBBB" in report
        assert "http://127.0.0.1:5640/" in report
        assert "witness pool down --name lab" in report, "cleanup must be one command away"

    def test_it_refuses_to_build_a_pool_over_a_pool_that_already_exists(self):
        docker = FakeDocker(script={"ps -a": _TWO_UP})
        pool, _ = _pool("up", docker=docker, count=2)

        with pytest.raises(PoolExists) as refusal:
            pool.run()

        assert "lab" in str(refusal.value)
        assert docker.matching("volume create") == [], "it must not have started building"

    def test_a_witness_that_never_becomes_ready_is_reported_and_left_for_inspection(self):
        docker = FakeDocker(script={"ps -a": ""})
        pool, _ = _pool("up", docker=docker, fetch=FakeFetch(), count=1, timeout=2)

        with pytest.raises(PoolNotReady) as refusal:
            pool.run()

        assert "w1" in str(refusal.value)
        assert docker.matching("rm -f") == [], "a failed pool is kept so its logs can be read"

    def test_a_witness_answering_health_without_ticks_is_not_yet_ready(self):
        """The loop turning is the readiness signal; an open database is not."""
        fetch = FakeFetch({"/v1/witness/health": {"status": "ok", "ticks": 0}})
        pool, _ = _pool("up", docker=FakeDocker(script={"ps -a": ""}), fetch=fetch,
                        count=1, timeout=2)

        with pytest.raises(PoolNotReady):
            pool.run()

    def test_it_needs_an_image_because_there_is_no_floating_tag_to_default_to(self):
        config = PoolConfig(verb="up", name="lab", count=1, image=None)
        pool = Pool(config, docker=FakeDocker(), fetch=FakeFetch(), out=[].append)

        with pytest.raises(InvalidArguments) as refusal:
            pool.run()

        assert "--image" in str(refusal.value)

    @pytest.mark.parametrize("count", [0, -1, 26])
    def test_the_witness_count_is_bounded(self, count):
        pool, _ = _pool("up", count=count)

        with pytest.raises(InvalidArguments):
            pool.run()

    def test_a_pool_whose_top_port_would_overflow_is_refused(self):
        pool, _ = _pool("up", count=4, base_port=65530)

        with pytest.raises(InvalidArguments) as refusal:
            pool.run()

        assert "65530" in str(refusal.value)


class TestDown:
    def test_it_removes_every_container_and_volume_the_pool_labelled(self):
        docker = FakeDocker(script={
            "ps -a": _TWO_UP,
            "volume ls": "witness-pool-lab-w1\nwitness-pool-lab-w2\n",
        })
        pool, written = _pool("down", docker=docker)

        assert pool.run() == 0
        assert "rm -f witness-pool-lab-w2 witness-pool-lab-w1" in docker.calls
        assert "volume rm -f witness-pool-lab-w1 witness-pool-lab-w2" in docker.calls
        assert "lab" in _printed(written)

    def test_it_removes_volumes_even_when_no_container_survived(self):
        """An `up` interrupted between the volume and the container still cleans up in full."""
        docker = FakeDocker(script={"ps -a": "", "volume ls": "witness-pool-lab-w1\n"})
        pool, _ = _pool("down", docker=docker)

        assert pool.run() == 0
        assert docker.matching("volume rm -f witness-pool-lab-w1")

    def test_downing_a_pool_that_does_not_exist_says_so(self):
        docker = FakeDocker(script={"ps -a": "", "volume ls": ""})
        pool, _ = _pool("down", docker=docker)

        with pytest.raises(PoolUnknown) as refusal:
            pool.run()

        assert "lab" in str(refusal.value)

    def test_all_sweeps_every_pool_on_the_host(self):
        docker = FakeDocker(script={
            "ps -a": "witness-pool-a-w1\tw1\t5640\t5642\trunning\twitness:test\n",
            "volume ls": "witness-pool-a-w1\n",
        })
        pool, _ = _pool("down", docker=docker, all_pools=True)

        assert pool.run() == 0
        assert docker.matching("--filter label=bakobo.pool ")


class TestStatus:
    def test_it_reports_each_witness_in_pool_order_with_what_it_holds(self):
        docker = FakeDocker(script={"ps -a": _TWO_UP})
        pool, written = _pool("status", docker=docker, fetch=FakeFetch(_healthy()))

        assert pool.run() == 0
        report = _printed(written)
        assert report.index("w1") < report.index("w2"), "docker's order is not the pool's"
        assert "ok" in report and "42" in report
        assert "1" in report, "the controller count is the multi-witness observation"

    def test_a_stopped_witness_reads_as_stopped_rather_than_as_a_failure(self):
        docker = FakeDocker(script={
            "ps -a": "witness-pool-lab-w1\tw1\t5640\t5642\texited\twitness:test\n"
        })
        pool, written = _pool("status", docker=docker, fetch=FakeFetch())

        assert pool.run() == 0
        assert "exited" in _printed(written)

    def test_a_running_witness_that_does_not_answer_is_unreachable(self):
        docker = FakeDocker(script={"ps -a": _TWO_UP})
        pool, written = _pool("status", docker=docker, fetch=FakeFetch())

        assert pool.run() == 0
        assert "unreachable" in _printed(written)

    def test_status_of_an_absent_pool_says_so(self):
        pool, _ = _pool("status", docker=FakeDocker(script={"ps -a": ""}))

        with pytest.raises(PoolUnknown):
            pool.run()


class TestBreakAndHeal:
    def test_break_stops_the_named_witness_by_default(self):
        docker = FakeDocker(script={"ps -a": _TWO_UP})
        pool, written = _pool("break", docker=docker, witness="w2")

        assert pool.run() == 0
        assert docker.matching("stop witness-pool-lab-w2")
        assert "w2" in _printed(written)

    def test_break_can_freeze_instead_of_stopping(self):
        docker = FakeDocker(script={"ps -a": _TWO_UP})
        pool, _ = _pool("break", docker=docker, witness="w1", mode="pause")

        assert pool.run() == 0
        assert docker.matching("pause witness-pool-lab-w1")

    def test_heal_reverses_whichever_way_the_witness_was_broken(self):
        stopped = FakeDocker(script={
            "ps -a": "witness-pool-lab-w1\tw1\t5640\t5642\texited\twitness:test\n"
        })
        pool, _ = _pool("heal", docker=stopped, witness="w1")
        assert pool.run() == 0
        assert stopped.matching("start witness-pool-lab-w1")

        paused = FakeDocker(script={
            "ps -a": "witness-pool-lab-w1\tw1\t5640\t5642\tpaused\twitness:test\n"
        })
        pool, _ = _pool("heal", docker=paused, witness="w1")
        assert pool.run() == 0
        assert paused.matching("unpause witness-pool-lab-w1")

    def test_healing_a_witness_that_is_already_running_changes_nothing(self):
        docker = FakeDocker(script={"ps -a": _TWO_UP})
        pool, written = _pool("heal", docker=docker, witness="w1")

        assert pool.run() == 0
        assert not docker.matching("start witness-pool-lab-w1")
        assert "already" in _printed(written)

    def test_breaking_a_witness_the_pool_does_not_have_names_the_ones_it_does(self):
        docker = FakeDocker(script={"ps -a": _TWO_UP})
        pool, _ = _pool("break", docker=docker, witness="w9")

        with pytest.raises(WitnessUnknown) as refusal:
            pool.run()

        assert "w1" in str(refusal.value) and "w2" in str(refusal.value)

    def test_break_needs_to_be_told_which_witness(self):
        pool, _ = _pool("break", docker=FakeDocker(script={"ps -a": _TWO_UP}))

        with pytest.raises(InvalidArguments):
            pool.run()


class TestManifest:
    def test_json_carries_every_witness_with_its_aid_oobi_and_urls(self):
        docker = FakeDocker(script={"ps -a": _TWO_UP})
        pool, written = _pool("manifest", docker=docker, fetch=FakeFetch(_healthy()))

        assert pool.run() == 0
        manifest = json.loads(_printed(written))
        assert manifest["pool"] == "lab"
        assert manifest["image"] == "witness:test"
        assert manifest["toad"] == 2
        assert manifest["witnesses"][0] == {
            "alias": "w1",
            "aid": "BAAA",
            "http": "http://127.0.0.1:5640/",
            "oobi": "http://127.0.0.1:5640/oobi/BAAA/controller",
            "control": "http://127.0.0.1:5642/",
        }

    def test_the_heti_format_is_the_standing_witness_set_block(self):
        docker = FakeDocker(script={"ps -a": _TWO_UP})
        pool, written = _pool("manifest", docker=docker, fetch=FakeFetch(_healthy()),
                              fmt="heti")

        assert pool.run() == 0
        assert _printed(written) == (
            "[witnesses]\n"
            "oobis = [\n"
            '  "http://127.0.0.1:5640/oobi/BAAA/controller",\n'
            '  "http://127.0.0.1:5650/oobi/BBBB/controller",\n'
            "]\n"
            "toad = 2\n"
        )

    def test_the_kli_format_is_an_incept_document_with_every_key_kli_requires(self):
        docker = FakeDocker(script={"ps -a": _TWO_UP})
        pool, written = _pool("manifest", docker=docker, fetch=FakeFetch(_healthy()),
                              fmt="kli")

        assert pool.run() == 0
        document = json.loads(_printed(written))
        assert set(document) == {
            "transferable", "wits", "toad", "icount", "isith", "ncount", "nsith"
        }
        assert document["wits"] == ["BAAA", "BBBB"]
        assert document["toad"] == 2

    def test_the_default_toad_is_keripys_own_ample_and_can_be_overridden(self):
        docker = FakeDocker(script={"ps -a": _TWO_UP})
        pool, written = _pool("manifest", docker=docker, fetch=FakeFetch(_healthy()), toad=1)

        assert pool.run() == 0
        assert json.loads(_printed(written))["toad"] == 1

    def test_a_witness_that_cannot_be_asked_its_aid_is_refused_rather_than_guessed(self):
        docker = FakeDocker(script={"ps -a": _TWO_UP})
        pool, _ = _pool("manifest", docker=docker, fetch=FakeFetch())

        with pytest.raises(PoolNotReady) as refusal:
            pool.run()

        assert "w1" in str(refusal.value)


class TestLs:
    def test_it_lists_every_pool_with_its_size_and_image(self):
        docker = FakeDocker(script={"ps -a": (
            "witness-pool-lab-w2\tlab\twitness:test\n"
            "witness-pool-lab-w1\tlab\twitness:test\n"
            "witness-pool-merti-w1\tmerti\twitness:other\n"
        )})
        pool, written = _pool("ls", docker=docker)

        assert pool.run() == 0
        report = _printed(written)
        assert "lab" in report and "merti" in report
        assert "witness:other" in report

    def test_no_pools_at_all_is_an_answer_rather_than_an_error(self):
        pool, written = _pool("ls", docker=FakeDocker(script={"ps -a": ""}))

        assert pool.run() == 0
        assert "no pools" in _printed(written).lower()


class TestDockerItself:
    def test_a_host_without_docker_says_so_retryably(self):
        docker = FakeDocker(fails=["ps -a"])
        pool, _ = _pool("status", docker=docker)

        with pytest.raises(DockerUnavailable) as refusal:
            pool.run()

        assert refusal.value.retryable


class TestTheDefaultSeams:
    """The real docker and HTTP seams, which the tests above replace and nothing else exercises."""

    def test_a_docker_command_returns_its_stdout(self, monkeypatch):
        from witness import pool as pool_mod

        class Result:
            returncode = 0
            stdout = "container-id\n"
            stderr = ""

        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            return Result()

        monkeypatch.setattr(pool_mod.subprocess, "run", fake_run)

        assert pool_mod.run_docker(["ps"]) == "container-id\n"
        assert seen["argv"] == ["docker", "ps"]

    def test_a_refused_command_carries_docker_s_own_reason(self, monkeypatch):
        from witness import pool as pool_mod

        class Result:
            returncode = 1
            stdout = ""
            stderr = "no such container: w9\n"

        monkeypatch.setattr(pool_mod.subprocess, "run", lambda argv, **kwargs: Result())

        with pytest.raises(DockerUnavailable) as refusal:
            pool_mod.run_docker(["stop", "w9"])

        assert "no such container: w9" in str(refusal.value)

    def test_a_failure_is_a_value_when_the_caller_said_it_was_not_news(self, monkeypatch):
        from witness import pool as pool_mod

        class Result:
            returncode = 1
            stdout = "partial\n"
            stderr = "already gone"

        monkeypatch.setattr(pool_mod.subprocess, "run", lambda argv, **kwargs: Result())

        assert pool_mod.run_docker(["volume", "rm", "gone"], check=False) == "partial\n"

    def test_a_host_with_no_docker_binary_says_which_command_was_missing(self, monkeypatch):
        from witness import pool as pool_mod

        def absent(argv, **kwargs):
            raise FileNotFoundError("docker")

        monkeypatch.setattr(pool_mod.subprocess, "run", absent)

        with pytest.raises(DockerUnavailable) as refusal:
            pool_mod.run_docker(["ps"])

        assert "docker command" in str(refusal.value)

    def test_a_docker_that_hangs_is_a_failure_rather_than_a_wait(self, monkeypatch):
        import subprocess as subprocess_mod

        from witness import pool as pool_mod

        def hangs(argv, **kwargs):
            raise subprocess_mod.TimeoutExpired(cmd=argv, timeout=1)

        monkeypatch.setattr(pool_mod.subprocess, "run", hangs)

        with pytest.raises(DockerUnavailable) as refusal:
            pool_mod.run_docker(["ps"], timeout=1)

        assert "within 1 seconds" in str(refusal.value)

    def test_a_control_plane_document_comes_back_parsed(self, monkeypatch):
        import contextlib
        import io

        from witness import pool as pool_mod

        @contextlib.contextmanager
        def fake_open(url, timeout=None):
            yield io.BytesIO(b'{"aid": "BAAA"}')

        monkeypatch.setattr(pool_mod.urllib.request, "urlopen", fake_open)

        assert pool_mod.fetch_json("http://127.0.0.1:5642/v1/witness/identity") == {"aid": "BAAA"}

    def test_a_witness_that_does_not_answer_is_none_rather_than_an_exception(self, monkeypatch):
        from witness import pool as pool_mod

        def refused(url, timeout=None):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(pool_mod.urllib.request, "urlopen", refused)

        assert pool_mod.fetch_json("http://127.0.0.1:5642/v1/witness/health") is None

    def test_without_an_out_the_pool_writes_to_stdout(self, capsys):
        from witness.pool import Pool

        pool = Pool(
            PoolConfig(verb="ls", name="lab"),
            docker=FakeDocker(script={"ps -a": ""}),
            fetch=FakeFetch(),
        )

        assert pool.run() == 0
        assert "no pools" in capsys.readouterr().out.lower()


class TestRaggedDockerOutput:
    """Docker's listings are text, and text has blank lines in it."""

    def test_blank_lines_in_a_container_listing_are_skipped(self):
        docker = FakeDocker(script={"ps -a": f"\n{_TWO_UP}\n"})
        pool, written = _pool("status", docker=docker, fetch=FakeFetch(_healthy()))

        assert pool.run() == 0
        assert _printed(written).count("w1") == 1

    def test_blank_lines_in_a_pool_listing_are_skipped(self):
        docker = FakeDocker(script={"ps -a": "\nwitness-pool-lab-w1\tlab\twitness:test\n\n"})
        pool, written = _pool("ls", docker=docker)

        assert pool.run() == 0
        assert "1 witnesses" in _printed(written)

    def test_a_pool_whose_volumes_are_already_gone_still_removes_its_containers(self):
        docker = FakeDocker(script={"ps -a": _TWO_UP, "volume ls": ""})
        pool, written = _pool("down", docker=docker)

        assert pool.run() == 0
        assert not docker.matching("volume rm")
        assert "0 volumes" in _printed(written)
