"""Dump the compliance vocabulary the API has to agree with.

    python -m scraper.compliance --dump-keys > compliance-keys.json

The worker and the API are written in different languages, so nothing can share
the definition. Committing this dump on the API side and comparing it there in
CI turns a divergence into a failing build rather than a production incident:
a status the API does not recognise fails request validation, and every lead
carrying it is reported as failed.

Issue keys and document statuses are dumped separately because they fail
differently — an unknown status is rejected by the API, while an unknown issue
key is stored and merely goes missing from the panel's filter.
"""

import argparse
import json

from . import COMPLIANCE_ISSUE_LABELS
from .legal_pages import DOCUMENT_STATUSES, DOCUMENTS


def contract() -> dict:
    """Return the vocabulary the worker can emit, sorted for a stable diff."""
    return {
        "issues": sorted(COMPLIANCE_ISSUE_LABELS),
        "document_statuses": sorted(DOCUMENT_STATUSES),
        "documents": sorted(DOCUMENTS),
    }


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m scraper.compliance", description=__doc__)
    parser.add_argument("--dump-keys", action="store_true",
                        help="print the issue keys, document ids and statuses as JSON")
    if not parser.parse_args().dump_keys:
        parser.error("nothing to do: pass --dump-keys")
    print(json.dumps(contract(), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
