"""witness error taxonomy (@zzbdxa).

Every failure is a typed :class:`WitnessError` carrying a stable code shaped
``<sorter>.<descriptor>[.<sub>].<disposition>``, per dev/standards/error-codes.md. The code is
classified by **what the obstacle was**, never by which component raised it, so a caller can
prefix-match a whole branch of meaning: ``e.env.*`` is "something we depend on did not deliver",
and ``e.*.r`` is "retrying could help" whatever the descriptor.

Over HTTP these render as RFC 9457 problem+json with media type ``application/problem+json``, per
dev/standards/http-errors.md. ``title`` is static per code and never varies with the occurrence;
``detail`` is this occurrence. There is deliberately no ``retryable`` member: the disposition is
the code's trailing token, and duplicating it in the body invites the two to disagree.

The predecessors of these codes (``witness.db.unavailable`` and friends) predate all four
standards. @zzbdxa chose to re-cut rather than grandfather them, on the grounds that witness is
private at 0.0.0 with no external consumer, so a freeze protects nobody today and protects more
with every consumer it acquires.
"""

from __future__ import annotations

_TYPE_BASE = "https://errors.bakobo.com"


class WitnessError(Exception):
    """Base for every witness failure.

    Subclasses set a stable :attr:`code`, a static :attr:`title`, and the HTTP :attr:`status` its
    code's prefix maps to. The message passed at the raise site becomes ``detail``.
    """

    code = "e.self.unknown.f"
    title = "Something failed and I could not attribute it."
    status = 500
    retryable = False

    def __init_subclass__(cls, **kwargs):
        """Derive retryability from the code at class-definition time.

        A class attribute rather than a property so callers can ask a *type* whether it is worth
        retrying without first constructing one — which is how a caller decides its policy. It is
        derived rather than declared so the code and the flag cannot drift apart.
        """
        super().__init_subclass__(**kwargs)
        cls.retryable = cls.code.endswith(".r")

    def __init__(self, detail, *, args=None):
        """``detail`` describes this occurrence; ``args`` are its situational values.

        http-errors.md defines ``args`` as "the situational values, positional, for clients that
        render their own text" — the point being that a client can localise or reformat without
        parsing the English out of ``detail``. Keyword-only and separate from ``Exception.args``,
        which already means something else and would collide.
        """
        super().__init__(detail)
        self.problem_args = list(args) if args else []

    def problem(self, instance=None, request_id=None) -> dict:
        """Render this error as an RFC 9457 problem document."""
        document = {
            "code": self.code,
            "type": f"{_TYPE_BASE}/{self.code}",
            "title": self.title,
            "detail": str(self),
        }
        if self.problem_args:
            document["args"] = self.problem_args
        if instance is not None:
            document["instance"] = instance
        if request_id is not None:
            document["request_id"] = request_id
        return document


class DbUnavailable(WitnessError):
    """The witness database could not be opened for reading."""

    code = "e.env.witnessdb.unavailable.r"
    title = "I could not open the witness database."
    status = 503


class WitnessNotIncepted(WitnessError):
    """The database opened but holds no identity at all — the witness has not been incepted yet.

    ~2lmg: distinct from :class:`ForeignKeystore` because the two have opposite dispositions.
    This one resolves on its own the moment the witness incepts, so it is retryable and a caller
    should wait rather than page anyone.
    """

    code = "e.state.pending.r"
    title = "The witness has not been incepted yet."
    status = 409


class ForeignKeystore(WitnessError):
    """The database holds identities, but none of them can be this witness's own.

    ~2lmg: a keystore belonging to some other controller is a deployment mistake, not a wait.
    Reporting another controller's AID as this witness's identity would be worse than reporting
    none, so this fails closed and says so.
    """

    code = "e.self.config.keystore.f"
    title = "The configured keystore does not belong to a witness."
    status = 500


class InvalidArguments(WitnessError):
    """The command-line arguments are invalid.

    ``e.input.`` because the obstacle is what was sent, even though it arrived on argv rather than
    over HTTP — the taxonomy classifies the obstacle, not the transport.
    """

    code = "e.input.format.f"
    title = "The arguments I was started with are not usable."
    status = 400


class TelemetryUnavailable(WitnessError):
    """The witness's telemetry segment could not be read.

    Retryable both ways it fails: the witness may not have started yet, or a read raced a write
    and lost. Neither needs anyone's attention.
    """

    code = "e.env.telemetry.unavailable.r"
    title = "I could not read the witness's telemetry."
    status = 503


class TelemetryIncompatible(WitnessError):
    """The telemetry segment exists but carries a layout this build cannot read.

    That means the two halves of one image were deployed at different versions, which retrying
    cannot fix.
    """

    code = "e.self.config.telemetry.f"
    title = "The witness's telemetry uses a layout I do not understand."
    status = 500


class ControllerUnknown(WitnessError):
    """This witness holds no key state for the requested AID.

    A genuinely absent resource rather than a failure: asking a witness about a controller it does
    not witness is a reasonable question with a negative answer.
    """

    code = "e.state.missing.controller.f"
    title = "I hold no key state for that controller."
    status = 404


class TelemetryNotConfigured(WitnessError):
    """This control plane was not told where the witness publishes telemetry.

    ``e.feature.`` rather than ``e.env.``: nothing is broken and nothing is missing at the other
    end — this deployment simply does not offer the capability, which is what a witness started by
    stock ``kli witness start`` looks like. Reporting it as a database failure, which an earlier
    version did, sends an operator to inspect a database that is perfectly healthy.
    """

    code = "e.feature.unsupported.telemetry.f"
    title = "This witness does not publish loop telemetry."
    status = 501


class RunnerNotRunning(WitnessError):
    """The witness runner process is not running, so its vitals cannot be read.

    Retryable because the supervisor restarts a crashed control plane and the orchestrator
    restarts a crashed witness, so the honest answer is "ask again" rather than "this failed".
    """

    code = "e.env.runner.unavailable.r"
    title = "The witness process is not running."
    status = 503
