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
from keri.core import Kevery, Parser, messagize, receipt


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

    def _incept_and_rotate(hab, rotations):
        """Witness a controller's inception plus `rotations` rotations; return its event SAIDs."""
        said_list = []
        _witness(hab.msgOwnInception(framed=True))
        said_list.append(hab.kever.serder.said)
        for sn in range(1, rotations + 1):
            hab.rotate()
            _witness(hab.msgOwnEvent(sn=sn))
            said_list.append(hab.kever.serder.said)
        return said_list

    saids = _incept_and_rotate(ctl_hab, rotations=1)

    # A second controller, with a longer history, so an index has more than one member and
    # ordering assertions have something to order.
    ctl2_hab = ctl_hby.makeHab(name="ctl2", transferable=True, wits=[wit_hab.pre], toad=1)
    saids2 = _incept_and_rotate(ctl2_hab, rotations=2)

    facts = {
        "head": head,
        "name": "witnessing",
        "witness_pre": wit_hab.pre,
        "witness_alias": wit_hab.name,
        "controller_pre": ctl_hab.pre,
        "saids": saids,
        "controller2_pre": ctl2_hab.pre,
        "saids2": saids2,
        # An AID that is well-formed and syntactically valid but which this witness has never
        # seen. The absent-AID path is what every data endpoint has to get right.
        "unwitnessed_pre": "EAAAAoAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "db_path": wit_hby.db.path,
    }
    ctl_hby.close()
    wit_hby.close()
    return facts


@pytest.fixture(scope="module")
def fully_witnessed_db(tmp_path_factory):
    """A witness LMDB holding a controller event receipted by TWO witnesses under toad=2.

    Kept separate from :func:`witnessing_db` because getting a *full* witness set into one
    witness's own wigs store needs the receipt-exchange round trip: each witness receipts
    independently, the controller collects both, and the combined receipt goes back out. A
    receipts endpoint sees this shape in any real deployment, and it is not the shape a
    single-witness fixture produces.
    """
    head = str(tmp_path_factory.mktemp("fullywitnessed"))

    wit1_hby = habbing.Habery(
        name="fullywitnessed", base="", temp=False, headDirPath=head, bran="abcdefghijk1234567890"
    )
    wit1_hab = wit1_hby.makeHab(name="wit1", transferable=False)
    wit1_kvy = Kevery(db=wit1_hab.db, lax=False, local=False)

    wit2_hby = habbing.Habery(name="wit2", base="", temp=True, bran="zyxwvutsrqp0987654321")
    wit2_hab = wit2_hby.makeHab(name="wit2", transferable=False)
    wit2_kvy = Kevery(db=wit2_hab.db, lax=False, local=False)

    ctl_hby = habbing.Habery(name="ctl3", base="", temp=True, bran="mnopqrstuvw1122334455")
    ctl_hab = ctl_hby.makeHab(
        name="ctl3", transferable=True, wits=[wit1_hab.pre, wit2_hab.pre], toad=2
    )
    ctl_kvy = Kevery(db=ctl_hab.db, lax=False, local=False)

    icp = ctl_hab.msgOwnInception(framed=True)

    # Each witness accepts the event and emits its own receipt.
    receipts = []
    for hab, kvy in ((wit1_hab, wit1_kvy), (wit2_hab, wit2_kvy)):
        Parser().parse(ims=bytearray(icp), kvy=kvy, local=True)
        receipts.append(hab.processCues(kvy.cues))

    # The controller collects both receipts, so its wigs hold the full set.
    for rct in receipts:
        Parser().parse(ims=bytearray(rct), kvy=ctl_kvy, local=True)

    said = ctl_hab.kever.serder.said
    wigers = ctl_hab.db.wigs.get(keys=(ctl_hab.pre, said))

    # Send the combined receipt back so our witness holds every witness's signature, not just
    # its own — the state a fully-witnessed event actually reaches.
    combined = messagize(
        serder=receipt(pre=ctl_hab.pre, sn=ctl_hab.kever.sn, said=said),
        wigers=wigers,
        framed=True,
    )
    Parser().parse(ims=bytearray(combined), kvy=wit1_kvy, local=True)

    facts = {
        "head": head,
        "name": "fullywitnessed",
        "controller_pre": ctl_hab.pre,
        "said": said,
        "witness_pres": [wit1_hab.pre, wit2_hab.pre],
        "toad": 2,
    }
    ctl_hby.close()
    wit2_hby.close()
    wit1_hby.close()
    return facts
