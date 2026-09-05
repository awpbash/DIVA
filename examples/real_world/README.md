# Real-world corpus

Nineteen real contracts, six real amendment chains, for testing this app
against actual legal drafting instead of the synthetic three-document demo.

## Source and licence

Every PDF here is a real exhibit filed with the U.S. Securities and Exchange
Commission, sourced from the [CUAD dataset](https://www.atticusprojectai.org/cuad)
(Contract Understanding Atticus Dataset, NeurIPS 2021), mirrored on
[Hugging Face](https://huggingface.co/datasets/theatticusproject/cuad). CUAD's
own curation and labels are CC BY 4.0. The underlying contracts are public
EDGAR filings, the dataset's own documentation makes no further warranty about
their copyright status beyond that, which is the norm for this category of
document (the same basis CUAD, its Hugging Face mirror, and its Kaggle mirror
already redistribute them on). Attribution is in [`NOTICE`](../../NOTICE).

Real company names, real dollar figures, real dates. Nothing here was written
for this repository, which is the point: it is the messiest and most honest
test this app has.

## The six chains

| Chain | Parties | Type | Documents |
| --- | --- | --- | --- |
| `glu-mobile/` | Glu Mobile (f/k/a Sorrent) / Fox Mobile Entertainment | Wireless content license | Base (2004) + 3 amendments (2005-2007) |
| `netgear-ingram/` | NETGEAR / Ingram Micro | Distributor agreement | Base (1996) + 2 amendments (1996, 1998) |
| `federated-services/` | Federated Investment Management / Federated Advisory Services | Services agreement | Base (2004) + 2 amendments (2009, 2016) |
| `pcquote-cobranding/` | PC Quote / A.B. Watley | Co-branding agreement | Base (1996) + 2 amendments (1996, 1998) |
| `bellring-manufacturing/` | Stremicks Heritage Foods / Premier Nutrition | Manufacturing agreement | Base (2017) + 3 amendments (2018-2019) |
| `neon-distributor/` | Peregrine/Bridge Transfer / Neon Systems | Distributor agreement | Base (1996) + 1 amendment (1999), the only pair here where the contract itself labels the parties "Licensor" and "Licensee" |

Each chain is independent. None reference each other, and each was picked
because a real amendment sequence is a much harder version of the same test
the synthetic demo runs: can the app find the current value of a field that a
later document changed, or correctly say nothing changed it.

## Loading it

```
python -m scripts.load_real_world_corpus
```

Run against a live instance (inside the app container if you're on Docker:
`docker compose exec app python -m scripts.load_real_world_corpus`). It
uploads all 19 files through the same intake path a browser upload uses,
waits for extraction to finish, and is idempotent, re-running after a
successful pass does nothing.

This is opt-in. It does not run at boot and does not touch the flagship
three-document demo, which stays the untouched, exact, auditable test it was
built to be. Loading real, considerably longer legal text costs real money:
the first ingestion of all 19 documents used roughly 1.5 million tokens
across extraction, several times the flagship demo's ~27,000. Every stage is
cached the same way, so a second run costs nothing.

## What this test actually found

Real legal drafting broke something the synthetic corpus never could have:
one contract's amendment identifies a party as "Glu Mobile Inc. f/k/a
Sorrent" ("formerly known as", a real and common way a contract refers to a
company that changed its name mid-relationship). The literal slash in "f/k/a"
was landing straight in the internal database record id for that party,
which Cosmos DB rejects, which broke the knowledge graph build for every
document processed after it, not just that one. Fixed in
`pipeline/kb/writers.py`'s `_canonical_key`, which now strips all punctuation
from a party's key, not only commas and periods. Covered by
`tests/kb/test_km.py::test_canonical_party_key_strips_slashes`.

Separately, this corpus caught that the Docker image never copied
`examples/` into itself at all, so the demo-loading path this whole
walkthrough depends on would have silently done nothing on any containerized
deployment. Fixed in the `Dockerfile`.

What worked without any change: the redaction and access-control behavior
held up identically at this larger scale, cross-chain retrieval stayed
correctly isolated (a question about one company's agreement did not pull in
another's), and the flagship demo's own three-document answers were
unaffected by sharing a knowledge base with 19 more documents.
