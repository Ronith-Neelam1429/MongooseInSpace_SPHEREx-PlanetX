"""Survey command line. From the repo root:

    uv run python -m planetx.survey init [--nside 64]
    uv run python -m planetx.survey status
    uv run python -m planetx.survey run --processor dry-run --db /tmp/scratch.sqlite
    uv run python -m planetx.survey requeue-failed
    uv run python -m planetx.survey requeue-unchecked

The database defaults to settings.db_path (env PLANETX_DB); `--db` overrides it.
"""
import argparse
import functools
import logging
import math
import sys
from pathlib import Path

from planetx.config import settings
from planetx.survey.patches import DEFAULT_NSIDE, NEP, nep_distance, validate_nside
from planetx.survey.queue import (
    DEFAULT_LEASE_S,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_RETRY_DELAY_S,
    NsideMismatchError,
    PatchQueue,
)
from planetx.survey.worker import PROCESSORS, run

log = logging.getLogger("planetx.survey")


def _cmd_init(args: argparse.Namespace) -> int:
    with PatchQueue(args.db, create=True) as queue:
        try:
            added = queue.init(args.nside)
        except NsideMismatchError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        stats = queue.stats()
    print(f"{args.db}: added {added} patches (nside {stats.nside}, {stats.total} total)")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    if not args.db.is_file():
        print(f"{args.db}: no survey database yet (run `init`)")
        return 1
    with PatchQueue(args.db, readonly=True) as queue:
        stats = queue.stats()
    if stats.total == 0:
        print(f"{args.db}: queue is empty (run `init`)")
        return 1
    print(f"{args.db}: nside {stats.nside}, {stats.total} patches")
    for status, n in stats.counts.items():
        print(f"  {status:<12}{n:>8}  ({100 * n / stats.total:.2f}%)")
    if stats.nside is None or stats.current_ipix is None:
        print("  frontier: finished")
    else:
        radius = float(nep_distance([stats.current_ipix], stats.nside)[0])
        print(f"  frontier: rank {stats.current_rank}, {radius:.2f} deg from the NEP "
              f"(RA {NEP.ra.deg:.2f}, Dec {NEP.dec.deg:.2f})")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    # The dry-run processor writes fake "done" results; keep it off the real survey.
    if args.processor == "dry-run" and args.db.resolve() == Path(settings.db_path).resolve():
        print("error: dry-run would mark real survey patches done; pass --db with a scratch database",
              file=sys.stderr)
        return 2
    processor = PROCESSORS[args.processor]
    if args.processor == "motion" and args.processes is not None:
        processor = functools.partial(processor, processes=args.processes)
    try:
        result = run(
            processor,
            args.db,
            max_patches=args.max_patches,
            lease_s=args.lease_s,
            max_attempts=args.max_attempts,
            retry_delay_s=args.retry_delay_s,
        )
    except (FileNotFoundError, RuntimeError) as exc:  # no database / not initialised
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"stopped: {result.reason}; done {result.done}, failed attempts {result.failed}, "
          f"released {result.released}, outage releases {result.outages}, lost leases {result.lease_lost}")
    return 130 if result.aborted else 0


def _cmd_requeue_failed(args: argparse.Namespace) -> int:
    try:
        queue = PatchQueue(args.db)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    with queue:
        n = queue.requeue_failed()
    print(f"{args.db}: requeued {n} failed patches")
    return 0


def _cmd_requeue_unchecked(args: argparse.Namespace) -> int:
    try:
        queue = PatchQueue(args.db)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    with queue:
        n = queue.requeue_unchecked()
    print(f"{args.db}: requeued {n} patches whose catalogue check did not run")
    return 0


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {value}")
    return value


def _positive_float(text: str) -> float:
    value = float(text)
    if not (math.isfinite(value) and value > 0):
        raise argparse.ArgumentTypeError(f"must be a finite number > 0, got {value}")
    return value


def _nside(text: str) -> int:
    try:
        return validate_nside(int(text))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m planetx.survey", description="AstroSearchX survey crawler")
    parser.add_argument("--db", type=Path, default=settings.db_path, help="survey SQLite database")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="create the patch queue (idempotent)")
    p_init.add_argument("--nside", type=_nside, default=DEFAULT_NSIDE, help="HEALPix nside, power of two")
    p_init.set_defaults(func=_cmd_init)

    p_status = sub.add_parser("status", help="show progress")
    p_status.set_defaults(func=_cmd_status)

    p_run = sub.add_parser("run", help="process patches until the queue is drained or Ctrl-C")
    p_run.add_argument("--processor", required=True, choices=sorted(PROCESSORS), help="patch processor")
    p_run.add_argument("--max-patches", type=_positive_int, default=None, help="stop after this many patches")
    p_run.add_argument("--lease-s", type=_positive_float, default=DEFAULT_LEASE_S)
    p_run.add_argument("--max-attempts", type=_positive_int, default=DEFAULT_MAX_ATTEMPTS)
    p_run.add_argument("--retry-delay-s", type=_positive_float, default=DEFAULT_RETRY_DELAY_S)
    p_run.add_argument("--processes", type=_positive_int, default=None,
                       help="motion processor: fields analysed at once, one process each "
                            "(default survey.motion.FIELD_PROCESSES, 4)")
    p_run.set_defaults(func=_cmd_run)

    p_requeue = sub.add_parser("requeue-failed", help="give failed patches a fresh set of attempts")
    p_requeue.set_defaults(func=_cmd_requeue_failed)

    p_unchecked = sub.add_parser(
        "requeue-unchecked",
        help="redo done patches analysed while Gaia was down (the motion worker also does this on its own)")
    p_unchecked.set_defaults(func=_cmd_requeue_unchecked)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
