"""witness error taxonomy.

Every witness failure is a typed :class:`WitnessError` carrying a stable symbolic code, a
``retryable`` flag (transient vs. permanent — whether retrying could help), and a complete,
plain-sentence message in the house voice. Error JSON bodies use the shape
``{"code": ..., "message": ..., "retryable": ...}``. See the Bakobo error-handling standard.
"""

from __future__ import annotations


class WitnessError(Exception):
    """Base for every witness failure.

    Subclasses set a stable :attr:`code` and a :attr:`retryable` flag. The message is a
    complete, plain sentence supplied at the raise site.
    """

    code = "witness.error"
    retryable = False

    def to_dict(self) -> dict:
        """Render this error as the frozen JSON error body."""
        return {"code": self.code, "message": str(self), "retryable": self.retryable}


class DbUnavailable(WitnessError):
    """The witness database could not be opened for reading.

    Transient: the witness may not be running yet, so retrying later could succeed.
    """

    code = "witness.db.unavailable"
    retryable = True


class IdentityUnavailable(WitnessError):
    """The witness database opened but has no witness identity yet.

    Transient: the witness may still be initializing, so retrying later could succeed.
    """

    code = "witness.identity.unavailable"
    retryable = True


class InvalidArguments(WitnessError):
    """The control-plane command-line arguments are invalid.

    Permanent: the same arguments will always be rejected, so retrying does not help.
    """

    code = "witness.config.invalid"
    retryable = False
