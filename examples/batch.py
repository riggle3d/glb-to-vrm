#!/usr/bin/env python3
"""Convert every .glb in a folder to VRM, as many at a time as the API allows.

    export RIGGLE_API_KEY=rgl_...
    python batch.py models/ --out vrm/

The API runs 3 conversions per account at once. This keeps that many in
flight: a 429 `too_many_active` means "wait for a slot", a 402
`insufficient_tokens` means stop uploading - what is already running still
finishes and is downloaded. A model whose .vrm already exists in the output
folder is skipped, so the script can be run again after topping up.

Each model costs 10 API tokens; a conversion that fails returns its tokens.
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))
from riggle import Riggle, RiggleError  # noqa: E402

POLL_SECONDS = 4


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("folder", type=Path, help="folder of .glb files")
    parser.add_argument("--out", type=Path,
                        help="where the .vrm files go (default: the same folder)")
    args = parser.parse_args()
    out = args.out or args.folder

    try:
        client = Riggle()
        account = client.account()
    except RiggleError as exc:
        sys.exit("Error: %s" % exc.message)
    slots = account.get("max_active_conversions") or 3
    price = (account.get("prices") or {}).get("model_to_vrm")

    queue = [p for p in sorted(args.folder.glob("*.glb"))
             if not (out / (p.stem + ".vrm")).exists()]
    if not queue:
        sys.exit("Nothing to convert in %s" % args.folder)
    print("%d models to convert, %s API tokens each; the account has %s."
          % (len(queue), price, account.get("api_tokens")))

    active = {}      # conversion id -> source path
    done, failed = [], []
    out_of_tokens = False
    said_waiting = False

    while queue or active:
        # Fill the free slots.
        while queue and len(active) < slots and not out_of_tokens:
            source = queue[0]
            try:
                conversion = client.create(file=str(source))
            except RiggleError as exc:
                if exc.code == "too_many_active":
                    # Something else on this account holds a slot (another
                    # script, an earlier run). Poll, then try again.
                    if not said_waiting:
                        print("The account already has its conversions in "
                              "flight elsewhere; waiting for a slot.")
                        said_waiting = True
                    break
                if exc.code == "insufficient_tokens":
                    out_of_tokens = True
                    print("Out of API tokens (%s needed, %s left): no more "
                          "uploads. Buy tokens at "
                          "https://app.riggle3d.com/developers/ and run this "
                          "again; finished models are skipped."
                          % (exc.detail("required"), exc.detail("available")))
                    break
                if exc.status in (400, 413):
                    # This file is the problem, not the batch.
                    queue.pop(0)
                    failed.append((source, exc.message))
                    print("refused  %s: %s" % (source.name, exc.message))
                    continue
                sys.exit("Error: %s" % exc.message)
            queue.pop(0)
            active[conversion["id"]] = source
            print("started  %s" % source.name)

        if out_of_tokens and not active:
            break
        time.sleep(POLL_SECONDS)

        # See what finished.
        for conversion_id, source in list(active.items()):
            try:
                conversion = client.get(conversion_id)
            except RiggleError as exc:
                print("          %s: %s (will ask again)"
                      % (source.name, exc.message))
                continue
            if conversion["status"] == "processing":
                continue
            del active[conversion_id]
            if conversion["status"] == "succeeded":
                target = out / (source.stem + ".vrm")
                client.download_vrm(conversion_id, target)
                done.append(source)
                print("done     %s -> %s" % (source.name, target))
                for warning in conversion.get("warnings") or []:
                    print("          warning: %s" % warning)
            else:
                message = (conversion.get("error") or {}).get("message", "failed")
                failed.append((source, message))
                print("failed   %s: %s (tokens returned)"
                      % (source.name, message))

    print("\n%d converted, %d failed, %d not started."
          % (len(done), len(failed), len(queue)))
    for source, message in failed:
        print("  %s: %s" % (source.name, message))
    sys.exit(1 if failed or queue else 0)


if __name__ == "__main__":
    main()
