"""Which domain this corpus demonstrates, and the intake declarations for
examples/real_world/corpus/. See examples/real_world/README.md for what
this set is and where it came from.

Same shape as examples/manifest.py, six independent chains instead of one.
Read by scripts/load_real_world_corpus.py, not by the setup wizard's Demo
path, this corpus is opt-in, not part of first boot.
"""
from __future__ import annotations

DOMAIN = "commercial_agreement"

CORPUS_DIR = "corpus"

# In upload order. `amends` names another entry's `file` (relative to
# CORPUS_DIR) by filename, resolved to that document's doc_id after
# ingesting it. Six chains, each rooted at its own base agreement, none
# reference each other.
CORPUS: tuple[dict, ...] = (
    # Glu Mobile / Fox Mobile: a wireless content license and three real
    # amendments filed over three years.
    {"file": "glu-mobile/01-wireless-content-license-2004.pdf",
     "document_type": "Agreement", "document_date": "2004-12-16"},
    {"file": "glu-mobile/02-amendment-1-2005.pdf",
     "document_type": "Amendment", "document_date": "2005-11-18",
     "relation": "amends", "amends": "glu-mobile/01-wireless-content-license-2004.pdf"},
    {"file": "glu-mobile/03-amendment-2-2006.pdf",
     "document_type": "Amendment", "document_date": "2006-03-27",
     "relation": "amends", "amends": "glu-mobile/01-wireless-content-license-2004.pdf"},
    {"file": "glu-mobile/04-amendment-3-2007.pdf",
     "document_type": "Amendment", "document_date": "2007-02-19",
     "relation": "amends", "amends": "glu-mobile/01-wireless-content-license-2004.pdf"},

    # NETGEAR / Ingram Micro distributor agreement and two amendments.
    {"file": "netgear-ingram/01-distributor-agreement-1996.pdf",
     "document_type": "Agreement", "document_date": "1996-11-05"},
    {"file": "netgear-ingram/02-amendment-1996.pdf",
     "document_type": "Amendment", "document_date": "1996-10-01",
     "relation": "amends", "amends": "netgear-ingram/01-distributor-agreement-1996.pdf"},
    {"file": "netgear-ingram/03-amendment-2-1998.pdf",
     "document_type": "Amendment", "document_date": "1998-07-15",
     "relation": "amends", "amends": "netgear-ingram/01-distributor-agreement-1996.pdf"},

    # Federated Investment Management / Federated Advisory Services.
    {"file": "federated-services/01-services-agreement-2004.pdf",
     "document_type": "Agreement", "document_date": "2004-01-01"},
    {"file": "federated-services/02-amendment-2009.pdf",
     "document_type": "Amendment", "document_date": "2009-03-30",
     "relation": "amends", "amends": "federated-services/01-services-agreement-2004.pdf"},
    {"file": "federated-services/03-second-amendment-2016.pdf",
     "document_type": "Amendment", "document_date": "2016-03-01",
     "relation": "amends", "amends": "federated-services/01-services-agreement-2004.pdf"},

    # PC Quote / A.B. Watley co-branding agreement and two amendments.
    {"file": "pcquote-cobranding/01-co-branding-agreement-1996.pdf",
     "document_type": "Agreement", "document_date": "1996-10-11"},
    {"file": "pcquote-cobranding/02-amendment-1996.pdf",
     "document_type": "Amendment", "document_date": "1996-12-09",
     "relation": "amends", "amends": "pcquote-cobranding/01-co-branding-agreement-1996.pdf"},
    {"file": "pcquote-cobranding/03-second-amendment-1998.pdf",
     "document_type": "Amendment", "document_date": "1998-02-23",
     "relation": "amends", "amends": "pcquote-cobranding/01-co-branding-agreement-1996.pdf"},

    # BellRing Brands / Stremicks Heritage Foods manufacturing agreement
    # and three amendments.
    {"file": "bellring-manufacturing/01-manufacturing-agreement-2017.pdf",
     "document_type": "Agreement", "document_date": "2017-07-01"},
    {"file": "bellring-manufacturing/02-amendment-1-2018.pdf",
     "document_type": "Amendment", "document_date": "2018-06-11",
     "relation": "amends", "amends": "bellring-manufacturing/01-manufacturing-agreement-2017.pdf"},
    {"file": "bellring-manufacturing/03-amendment-2-2018.pdf",
     "document_type": "Amendment", "document_date": "2018-10-01",
     "relation": "amends", "amends": "bellring-manufacturing/01-manufacturing-agreement-2017.pdf"},
    {"file": "bellring-manufacturing/04-amendment-3-2019.pdf",
     "document_type": "Amendment", "document_date": "2019-07-03",
     "relation": "amends", "amends": "bellring-manufacturing/01-manufacturing-agreement-2017.pdf"},

    # Neon Systems / Peregrine distributor agreement and one amendment.
    # The only pair in this set where the contract itself labels the
    # parties "Licensor" and "Licensee".
    {"file": "neon-distributor/01-distributor-agreement-1996.pdf",
     "document_type": "Agreement", "document_date": "1996-01-01"},
    {"file": "neon-distributor/02-first-amendment-1999.pdf",
     "document_type": "Amendment", "document_date": "1999-01-01",
     "relation": "amends", "amends": "neon-distributor/01-distributor-agreement-1996.pdf"},
)
