# Real-world corpus

This optional corpus contains 19 public contracts from SEC filings, arranged as
six independent amendment chains. It is larger and less uniform than the
synthetic sample, so it is useful for testing ingestion and retrieval against
real contract formatting.

## Source and licence

The files come from the [CUAD dataset](https://www.atticusprojectai.org/cuad),
which is also mirrored on [Hugging Face](https://huggingface.co/datasets/theatticusproject/cuad).
The dataset materials are distributed under CC BY 4.0. Attribution is recorded
in [`NOTICE`](../../NOTICE). Review the source terms before redistributing the
files outside this repository.

## The chains

| Directory | Documents | Type |
| --- | ---: | --- |
| `glu-mobile/` | 4 | Wireless content licence |
| `netgear-ingram/` | 3 | Distributor agreement |
| `federated-services/` | 3 | Services agreement |
| `pcquote-cobranding/` | 3 | Co-branding agreement |
| `bellring-manufacturing/` | 4 | Manufacturing agreement |
| `neon-distributor/` | 2 | Distributor agreement |

Each chain is independent. The files are selected to exercise current-value
lookups across amendments, party identity, page geometry, and cross-document
retrieval.

## Load it

The corpus is opt-in and is not loaded when DIVA starts:

```bash
python -m scripts.load_real_world_corpus
```

When using Docker, run it inside the app container:

```bash
docker compose exec app python -m scripts.load_real_world_corpus
```

The first run sends the documents through the configured reader and model
endpoint, so it can incur model charges. Later runs reuse the cached artifacts.
Use the synthetic corpus first when checking a new code or configuration
change.

## What it is good for

- testing OCR and page geometry on varied scans;
- checking that chains remain separate in retrieval;
- inspecting how party names and amendment wording vary; and
- finding cases that deserve a new regression test.

Do not include private documents or API keys in new examples, issues, or test
fixtures. For domain-specific field expectations, add a small gold set through
the evaluation tools instead of treating this corpus as legal advice.
