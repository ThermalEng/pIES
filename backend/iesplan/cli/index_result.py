"""Build the bounded result read index for an already sealed evidence package.

Docker deployment command (no solver, input/authority rewrite, or DB migration):
python -m iesplan.cli.index_result --evidence-package-id <id>
"""

import argparse
import json

from iesplan.application.results import index_evidence_projection
from iesplan.db import SessionLocal


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-package-id", type=int, required=True)
    args = parser.parse_args()
    with SessionLocal() as db:
        object_id = index_evidence_projection(db, args.evidence_package_id)
    print(json.dumps({"evidence_package_id": args.evidence_package_id, "projection_object_id": object_id}))


if __name__ == "__main__":
    main()
