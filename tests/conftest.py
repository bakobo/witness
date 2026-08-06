"""Shared fixtures.

The important one here is :func:`witnessing_db`: a witness LMDB that has actually witnessed
something. Every fixture that predates it builds only the witness's own hab, which is all
``/info`` ever needed — but every endpoint that serves witnessed data needs a database with
another controller's events received, receipted, and first-seen-indexed in it.

Valid KERI material is built with keripy's own Habery/eventing rather than hand-rolled, which is
the same constraint heti records at ``@4zqk7d`` for ``heti.testing``: a second implementation of
event construction would share bugs with the first and hide them in both.
"""

import pytest
from keri.app import habbing
from keri.core import Kevery, Parser


@pytest.fixture(scope="module")
def witnessing_db(tmp_path_factory):
    """A persistent witness LMDB that has received, receipted, and first-seen-indexed the
    inception and a rotation of a separate controller AID.

    Returns the facts a test needs to make assertions without reopening the writer: the
    witness's own prefix and alias, the witnessed controller's prefix, its event SAIDs in
    sequence order, and the head directory the reader should be pointed at.
    """
    head = str(tmp_path_factory.mktemp("witnessing"))

    # The witness: non-transferable, persistent, reopenable read-only.
    wit_hby = habbing.Habery(
        name="witnessing", base="", temp=False, headDirPath=head, bran="abcdefghijk1234567890"
    )
    wit_hab = wit_hby.makeHab(name="wit", transferable=False)

    # A non-local Kevery is what accepts another controller's events and cues the receipts.
    wit_kvy = Kevery(db=wit_hab.db, lax=False, local=False)

    # The controller, in its own keystore, designating our witness.
    ctl_hby = habbing.Habery(name="controller", base="", temp=True, bran="0987654321kjihgfedcba")
    ctl_hab = ctl_hby.makeHab(name="ctl", transferable=True, wits=[wit_hab.pre], toad=1)

    saids = []

    def _witness(msg):
        """Feed one controller message to the witness and let it emit its receipt."""
        Parser().parse(ims=bytearray(msg), kvy=wit_kvy, local=True)
        rct = wit_hab.processCues(wit_kvy.cues)
        # Feed the receipt back so the witness's own wigs store holds its signature.
        if rct:
            Parser().parse(ims=bytearray(rct), kvy=wit_kvy, local=True)
        return rct

    _witness(ctl_hab.msgOwnInception(framed=True))
    saids.append(ctl_hab.kever.serder.said)

    ctl_hab.rotate()
    _witness(ctl_hab.msgOwnEvent(sn=1))
    saids.append(ctl_hab.kever.serder.said)

    facts = {
        "head": head,
        "name": "witnessing",
        "witness_pre": wit_hab.pre,
        "witness_alias": wit_hab.name,
        "controller_pre": ctl_hab.pre,
        "saids": saids,
        "db_path": wit_hby.db.path,
    }
    ctl_hby.close()
    wit_hby.close()
    return facts
