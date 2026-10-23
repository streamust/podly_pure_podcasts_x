#!/usr/bin/env python3
"""Capture a sting template for the sting anchor (sting_anchor.py).

Extracts a short slice of an episode file as a canonical template WAV
(mono, 8kHz, 16-bit PCM) and prints the sting-config JSON snippet for
the feed, with the role parameters that a 16-episode ear-reviewed
backtest validated. The one value this script cannot give you is the
per-show ``offset`` -- the delay from sting onset to the true ad
boundary. Measure it on one known-good break per role:

    offset = (true ad start) - (sting onset)     for the start role
    offset = (true ad end)   - (return onset)    for the end role

Worked examples from the backtest: a short sting that plays under the
transition sits ~4.4s before the ad start; a long music bed's opening
motif sits ~13.6s before it; a return sting's resume cut lands ~1.0s
before the onset (offset -1.0), so the sting stays audible.

Template hygiene (learned the hard way): the slice must be music-only
-- voiceover over the template drops its match score from ~1.0 to
~0.2. Short stings want their clean core (2s can beat 4s); long beds
want their first ~7s from the transition onset, never a mid-bed slice.

Usage:
    capture_sting_template.py EPISODE_FILE START END OUTPUT.wav \
        [--feed-id N] [--roles start,end]

Auto-duration calibration (shrinking the slice while cross-episode
correlation holds) is a deliberate follow-up, not part of this tool.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

ROLE_SNIPPETS = {
    "start": {
        "offset": 0.0,
        "window_before": 45.0,
        "window_after": 8.0,
        "min_move": 1.0,
        "regime_gate": [6.0, 15.0],
    },
    "end": {
        "offset": -1.0,
        "window_before": 25.0,
        "window_after": 20.0,
        "dead_zone": 3.0,
        "min_move": 1.0,
        "later_move_min_score": 0.9,
        "min_start_gap": 15.0,
    },
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("episode", help="episode audio file (any ffmpeg format)")
    parser.add_argument("start", type=float, help="template start, seconds")
    parser.add_argument("end", type=float, help="template end, seconds")
    parser.add_argument("output", help="output template WAV path")
    parser.add_argument("--feed-id", default="FEED_ID", help="Podly feed id")
    parser.add_argument(
        "--roles",
        default="start,end",
        help="comma-separated roles for the printed snippet (default: start,end)",
    )
    args = parser.parse_args()

    if args.end <= args.start:
        parser.error("END must be after START")

    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-ss",
            str(args.start),
            "-t",
            str(args.end - args.start),
            "-i",
            args.episode,
            "-ac",
            "1",
            "-ar",
            "8000",
            "-sample_fmt",
            "s16",
            args.output,
        ],
        check=True,
    )
    print(
        f"wrote template {args.output} ({args.end - args.start:.2f}s)", file=sys.stderr
    )

    entry: dict[str, object] = {"template": args.output, "threshold": 0.7}
    for raw_role in args.roles.split(","):
        role = raw_role.strip()
        if role not in ROLE_SNIPPETS:
            parser.error(f"unknown role {role!r} (expected start or end)")
        entry[role] = ROLE_SNIPPETS[role]
    snippet = {"feeds": {str(args.feed_id): entry}}
    print(json.dumps(snippet, indent=2))
    print(
        "NOTE: set each role's 'offset' from a known-good break before "
        "enabling the feed (offset 0.0 above is a placeholder).",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
