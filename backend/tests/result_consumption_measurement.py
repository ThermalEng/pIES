"""Docker-only bounded-memory projection measurement over a synthetic JSON document.

Example, from backend in a current-code test image limited to 1 GiB RAM/no swap:
python -m tests.result_consumption_measurement --megabytes 1536
No database, case data, solver, or complete in-memory input is used.
"""

import argparse
import json
import resource
import time

from iesplan.core.content import GeneratedContent
from iesplan.core.jsonstream import ObjectFields, project_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--megabytes", type=int, default=1536)
    args = parser.parse_args()

    def chunks():
        yield b'{"step_values":['
        block = b"0," * 32768
        for _ in range(args.megabytes * 16):
            yield block
        yield b'1],"metrics":{"npv":"12.34","unit":"CNY"}}'

    started = time.monotonic()
    projected = project_json(GeneratedContent(chunks), ObjectFields({"metrics": True}))
    assert projected == {"metrics": {"npv": "12.34", "unit": "CNY"}}
    print(json.dumps({"input_mib": args.megabytes, "elapsed_seconds": time.monotonic() - started,
                      "peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024}))


if __name__ == "__main__":
    main()
