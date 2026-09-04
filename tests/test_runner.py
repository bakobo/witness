"""``witness run`` — the launcher (@n5r2vq), and the contract guarding what it reproduces.

The launcher mirrors keripy's own ``runWitness`` up to its last line, because that last line —
``runController`` — is the one that has to change so the Doist can be ours. Reproducing anything
from an unforked upstream dependency is a drift risk that @w7c4mz says to pair with contract
tests, so the symbols that reproduction stands on are asserted here.
"""

import inspect

import pytest
from keri.app import Configer, Habery, HaberyDoer, Keeper, indirecting
from keri.cli import common as keri_cli_common

from witness import inloop, runner, telemetry
from witness import config as config_mod
from witness.config import RunnerConfig


@pytest.fixture
def config(tmp_path):
    return RunnerConfig(
        name="testwit",
        alias="testwit",
        base="",
        passcode=None,
        config_dir=None,
        config_file=None,
        tcp_port=5632,
        http_port=5631,
        telemetry_path=str(tmp_path / "telemetry"),
    )


class FakeDoer:
    def __init__(self, name):
        self.__class__ = type(name, (FakeDoer,), {}) if name else self.__class__


class FakeDoist:
    instances = []

    def __init__(self, *, limit, tock, real, doers, sink):
        self.limit, self.tock, self.real, self.doers, self.sink = limit, tock, real, doers, sink
        self.ran = False
        FakeDoist.instances.append(self)

    @staticmethod
    def names_for(doers):
        return inloop.TimedDoist.names_for(doers)

    def do(self):
        self.ran = True


def test_run_assembles_stock_doers_and_runs_them_in_a_timed_doist(config):
    FakeDoist.instances.clear()
    stock = [inloop.TelemetryDoer.__new__(inloop.TelemetryDoer)]
    built = {}

    def fake_build(cfg, hby, **_kwargs):
        built["hby"] = hby
        return list(stock)

    subject = runner.WitnessRunner(
        config,
        open_habery=lambda cfg: "the-habery",
        build_doers=fake_build,
        doist_factory=FakeDoist,
    )

    assert subject.run() == 0

    doist = FakeDoist.instances[-1]
    assert doist.ran is True
    assert doist.tock == runner.TOCK
    assert doist.real is True
    assert built["hby"] == "the-habery"


def test_the_telemetry_doer_is_appended_and_named_in_the_segment(config):
    FakeDoist.instances.clear()
    runner.WitnessRunner(
        config,
        open_habery=lambda cfg: None,
        build_doers=lambda cfg, hby, **_k: [],
        doist_factory=FakeDoist,
    ).run()

    names = [d["name"] for d in telemetry.SegmentReader(config.telemetry_path).read()["doers"]]
    assert names == ["TelemetryDoer"]
    assert isinstance(FakeDoist.instances[-1].doers[-1], inloop.TelemetryDoer)


def test_the_segment_gets_one_slot_per_doer_including_the_telemetry_doer(config):
    FakeDoist.instances.clear()

    class Alpha(inloop.doing.Doer):
        pass

    class Beta(inloop.doing.Doer):
        pass

    runner.WitnessRunner(
        config,
        open_habery=lambda cfg: None,
        build_doers=lambda cfg, hby, **_k: [Alpha(), Beta()],
        doist_factory=FakeDoist,
    ).run()

    names = [d["name"] for d in telemetry.SegmentReader(config.telemetry_path).read()["doers"]]
    assert names == ["Alpha", "Beta", "TelemetryDoer"]


def test_build_doers_puts_a_haberydoer_ahead_of_the_stock_witness_doers(config):
    seen = {}

    def fake_setup_witness(**kwargs):
        seen.update(kwargs)
        return ["stock-a", "stock-b"]

    doers = runner.build_doers(config, hby="H", setup_witness=fake_setup_witness)

    assert isinstance(doers[0], HaberyDoer)
    assert doers[1:] == ["stock-a", "stock-b"]
    assert seen == {"alias": "testwit", "hby": "H", "tcpPort": 5632, "httpPort": 5631}


# ------------------------------------------------------------------------------------------
# Contract over what the launcher reproduces from keripy (@w7c4mz)
# ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "symbol", [Configer, Habery, HaberyDoer, Keeper, keri_cli_common.setupHby]
)
def test_the_symbols_runwitness_uses_still_exist(symbol):
    assert symbol is not None


def test_setup_witness_still_takes_the_ports_the_launcher_passes():
    parameters = inspect.signature(indirecting.setupWitness).parameters
    for expected in ("hby", "alias", "tcpPort", "httpPort"):
        assert expected in parameters, (
            f"keripy's setupWitness no longer takes {expected!r}; the launcher reproduces "
            f"runWitness's call and must be updated with it"
        )


def test_setup_hby_still_takes_the_keystore_arguments_the_launcher_passes():
    parameters = inspect.signature(keri_cli_common.setupHby).parameters
    for expected in ("name", "base", "bran", "cf"):
        assert expected in parameters


def test_the_keeper_still_answers_for_the_aeid_that_selects_the_open_path(tmp_path):
    """open_habery branches on whether the keystore is encrypted, read from gbls['aeid'] — the
    same probe runWitness makes. If that key moves, the launcher silently takes the wrong branch.
    """
    keeper = Keeper(name="probe", base="", temp=True, reopen=True)
    try:
        assert keeper.gbls.get("aeid") is None
    finally:
        keeper.close()


def test_a_real_unencrypted_keystore_reports_an_empty_aeid_not_none(tmp_path):
    """The branch in open_habery hinges on this, and it is not what the name suggests.

    `kli init --nopasscode` leaves aeid as '' — verified against a keystore built by the real CLI
    in the image — so an ordinary unencrypted witness takes the setupHby path, and the `is None`
    branch means "no keystore at all". Reading it as "is unencrypted" would invert the logic.
    """
    from keri.app import habbing

    head = str(tmp_path / "keristore")
    hby = habbing.Habery(name="aeidprobe", base="", temp=False, headDirPath=head)
    hby.close()

    keeper = Keeper(name="aeidprobe", base="", temp=False, reopen=True, headDirPath=head)
    try:
        assert keeper.gbls.get("aeid") == ""
    finally:
        keeper.close()


def test_open_habery_takes_the_setuphby_path_when_the_keystore_is_encrypted(tmp_path, monkeypatch):
    """An encrypted keystore stores an aeid, and keripy unlocks it through setupHby rather than
    by constructing a Habery — taking the wrong branch there would fail at first use, not here."""

    class FakeKeeper:
        def __init__(self, **kwargs):
            self.gbls = {"aeid": "EBoguSaeid"}

        def close(self):
            self.closed = True

    monkeypatch.setattr(runner, "Keeper", FakeKeeper)
    monkeypatch.setattr(runner, "setupHby", lambda **kwargs: ("setupHby", kwargs))

    cfg = RunnerConfig(
        name="enc", alias="enc", base="b", passcode="0123456789abcdefghijk",
        config_dir=None, config_file=None, tcp_port=1, http_port=2,
        telemetry_path=str(tmp_path / "t"),
    )
    kind, kwargs = runner.open_habery(cfg)

    assert kind == "setupHby"
    assert kwargs["bran"] == "0123456789abcdefghijk"


def test_open_habery_loads_a_config_file_when_one_is_named(tmp_path, monkeypatch):
    seen = {}

    class FakeKeeper:
        def __init__(self, **kwargs):
            self.gbls = {"aeid": None}

        def close(self):
            pass

    monkeypatch.setattr(runner, "Keeper", FakeKeeper)
    monkeypatch.setattr(runner, "Configer", lambda **kwargs: seen.setdefault("configer", kwargs))
    monkeypatch.setattr(runner, "Habery", lambda **kwargs: seen.setdefault("habery", kwargs))

    cfg = RunnerConfig(
        name="c", alias="c", base="", passcode=None, config_dir="/cfgdir", config_file="wit",
        tcp_port=1, http_port=2, telemetry_path=str(tmp_path / "t"),
    )
    runner.open_habery(cfg)

    assert seen["configer"]["name"] == "wit"
    assert seen["configer"]["headDirPath"] == "/cfgdir"
    assert seen["habery"]["cf"] is not None


def test_the_escrow_timeout_is_applied_before_the_witness_doers_are_built(config, monkeypatch):
    """@znm5uppx. keripy reads TimeoutQNF off the CLASS when its Kevery runs, so setting it after
    setupWitness would still work — but setting it before is the only ordering that is obviously
    correct, and this pins it. Sustained escrow depth is the attacker's rate times this number."""
    from keri.core.eventing import Kevery

    original = Kevery.TimeoutQNF
    order = []

    def fake_build(cfg, hby, **_kwargs):
        order.append(("build", Kevery.TimeoutQNF))
        return []

    FakeDoist.instances.clear()
    try:
        runner.WitnessRunner(
            config,
            open_habery=lambda cfg: order.append(("habery", Kevery.TimeoutQNF)),
            build_doers=fake_build,
            doist_factory=FakeDoist,
        ).run()
        assert order == [("habery", config.escrow_timeout), ("build", config.escrow_timeout)]
    finally:
        Kevery.TimeoutQNF = original


def test_the_default_escrow_timeout_is_well_below_keripys():
    """The measurement is the argument: keripy's 300 s makes sustained depth five times larger for
    the same attack rate, and cost is linear in depth."""
    from keri.core.eventing import Kevery

    _subcommand, cfg = config_mod.parse_args(["run", "--name", "w"])

    assert cfg.escrow_timeout == 60
    assert cfg.escrow_timeout < Kevery.TimeoutQNF or Kevery.TimeoutQNF == 60


def test_the_escrow_sweep_is_paced_after_the_doers_are_built(config, monkeypatch):
    """@zj3h2pzh. The substitution has exactly one valid window: after setupWitness has built
    WitnessStart.doers and before the Doist enters and reads them."""
    seen = {}
    monkeypatch.setattr(
        runner.escrows, "pace",
        lambda doers, interval: seen.update(doers=list(doers), interval=interval),
    )
    FakeDoist.instances.clear()
    marker = inloop.doing.Doer()

    runner.WitnessRunner(
        config,
        open_habery=lambda cfg: None,
        build_doers=lambda cfg, hby, **_k: [marker],
        doist_factory=FakeDoist,
    ).run()

    assert seen["doers"] == [marker], "pacing must see the stock doers, before ours are appended"
    assert seen["interval"] == config.escrow_interval


def test_the_default_escrow_interval_is_far_inside_the_escrow_lifetime():
    """A sweep interval approaching the escrow timeout would let entries expire unswept, which
    trades a cost problem for a correctness one."""
    _subcommand, cfg = config_mod.parse_args(["run", "--name", "w"])

    assert cfg.escrow_interval == 1.0
    assert cfg.escrow_interval * 10 < cfg.escrow_timeout


def test_stock_escrow_behaviour_can_be_restored_from_the_command_line():
    _subcommand, cfg = config_mod.parse_args(["run", "--name", "w", "--escrow-interval", "0"])

    assert cfg.escrow_interval == 0
