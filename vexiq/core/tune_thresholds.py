"""CLI utility and simulation engine for threshold tuning.

Allows manual labeling of VexIQ mistakes to track precision/recall metrics,
and simulates hypothetical threshold parameter shifts.
"""

import argparse
import asyncio
import re
import sys
from datetime import datetime, timezone
from typing import Any

from vexiq.config import get_settings
from vexiq.db import get_db_conn, set_event_label, init_db


def parse_metrics(detection_signal: str, failure_summary: str) -> float | None:
    """Parses numeric metrics out of failure summary strings."""
    if not failure_summary:
        return None
    sig = detection_signal.lower()
    if sig == "heavy_edit":
        match = re.search(r"edit ratio:\s*([0-9.]+)%", failure_summary)
        if match:
            return float(match.group(1)) / 100.0
    elif sig == "manual_rewrite":
        match = re.search(r"rewrite ratio:\s*([0-9.]+)%", failure_summary)
        if match:
            return float(match.group(1)) / 100.0
    elif sig == "immediate_retry":
        match = re.search(r"after\s*([0-9.]+)s", failure_summary)
        if match:
            return float(match.group(1))
    return None


async def get_unlabeled_mistakes(db_path: str) -> list[dict[str, Any]]:
    """Gets all logged mistakes that have not been manually labeled."""
    query = """
        SELECT m.* FROM ai_mistakes m
        LEFT JOIN labeled_events l ON m.mistake_id = l.event_id
        WHERE l.event_id IS NULL
        ORDER BY m.timestamp ASC
    """
    mistakes = []
    async with get_db_conn(db_path) as db:
        async with db.execute(query) as cursor:
            rows = await cursor.fetchall()
            for row in rows:
                mistakes.append(dict(row))
    return mistakes


async def get_all_labeled_mistakes(db_path: str) -> list[dict[str, Any]]:
    """Gets all labeled events joined with their mistake metadata."""
    query = """
        SELECT l.label, m.mistake_id, m.task_type, m.detection_signal, m.failure_summary, m.timestamp
        FROM labeled_events l
        JOIN ai_mistakes m ON l.event_id = m.mistake_id
        WHERE l.event_type = 'mistake'
    """
    mistakes = []
    async with get_db_conn(db_path) as db:
        async with db.execute(query) as cursor:
            rows = await cursor.fetchall()
            for row in rows:
                mistakes.append(dict(row))
    return mistakes


async def get_labeled_missed_mistakes(db_path: str) -> list[dict[str, Any]]:
    """Gets all labeled missed mistakes (events manually flagged by users but missed by detectors)."""
    query = """
        SELECT l.label, l.event_id as decision_id, d.task_type, d.provider, d.model_id, d.timestamp
        FROM labeled_events l
        JOIN ai_decisions d ON l.event_id = d.decision_id
        WHERE l.event_type = 'decision' AND l.label = 'missed_mistake'
    """
    misses = []
    async with get_db_conn(db_path) as db:
        async with db.execute(query) as cursor:
            rows = await cursor.fetchall()
            for row in rows:
                misses.append(dict(row))
    return misses


async def cmd_label(db_path: str) -> None:
    """Interactively labels unlabeled mistakes in the terminal."""
    unlabeled = await get_unlabeled_mistakes(db_path)
    if not unlabeled:
        print("No unlabeled mistakes found in database.")
        return

    print(f"Found {len(unlabeled)} unlabeled mistakes. Starting labeling session...\n")
    for i, m in enumerate(unlabeled, 1):
        print("=" * 60)
        print(f"Mistake {i}/{len(unlabeled)}")
        print(f"ID:               {m['mistake_id']}")
        print(f"Timestamp:        {m['timestamp']}")
        print(f"Provider/Model:   {m['provider']} / {m['model_id']}")
        print(f"Task Type:        {m['task_type']}")
        print(f"Detection Signal: {m['detection_signal']}")
        print(f"Failure Summary:  {m['failure_summary']}")
        print("-" * 60)

        # Parse metrics for helper display
        val = parse_metrics(m["detection_signal"], m["failure_summary"])
        if val is not None:
            print(f"Parsed Metric Value: {val}")

        while True:
            choice = input(
                "Label: [t]rue mistake, [f]alse positive, [m]issed mistake, [a]mbiguous, [s]kip, [q]uit: "
            ).strip().lower()

            if choice == "q":
                print("Exiting labeling session.")
                return
            elif choice == "s":
                print("Skipped.")
                break
            elif choice in ("t", "f", "m", "a"):
                label_map = {
                    "t": "true_mistake",
                    "f": "false_positive",
                    "m": "missed_mistake",
                    "a": "ambiguous",
                }
                label = label_map[choice]
                await set_event_label(db_path, m["mistake_id"], "mistake", label)
                print(f"Logged label as: {label}")
                break
            else:
                print("Invalid input. Please choose t, f, m, a, s, or q.")
        print()


async def cmd_stats(db_path: str) -> None:
    """Computes and displays precision and recall metrics based on labeled events."""
    labeled_mistakes = await get_all_labeled_mistakes(db_path)
    labeled_misses = await get_labeled_missed_mistakes(db_path)

    if not labeled_mistakes and not labeled_misses:
        print("No labeled events found. Run the 'label' command first.")
        return

    # Aggregate stats per signal + task type
    stats: dict[str, dict[str, int]] = {}

    def get_stat_bucket(task_type: str, signal: str) -> dict[str, int]:
        key = f"{task_type} | {signal}"
        if key not in stats:
            stats[key] = {
                "true_positives": 0,
                "false_positives": 0,
                "missed_mistakes": 0,
                "ambiguous": 0,
            }
        return stats[key]

    for m in labeled_mistakes:
        bucket = get_stat_bucket(m["task_type"], m["detection_signal"])
        label = m["label"]
        if label == "true_mistake":
            bucket["true_positives"] += 1
        elif label == "false_positive":
            bucket["false_positives"] += 1
        elif label == "ambiguous":
            bucket["ambiguous"] += 1
        elif label == "missed_mistake":
            # Just in case a mistake was manually flagged as a miss
            bucket["missed_mistakes"] += 1

    for miss in labeled_misses:
        # Misses don't have a detection signal because they weren't caught
        bucket = get_stat_bucket(miss["task_type"], "undetected")
        bucket["missed_mistakes"] += 1

    print("\n" + "=" * 80)
    print(f"{'Signal & Task Type':<35} | {'TP':<4} | {'FP':<4} | {'Miss':<4} | {'Precision':<10} | {'Recall':<10}")
    print("-" * 80)

    total_tp = 0
    total_fp = 0
    total_miss = 0

    for key, s in sorted(stats.items()):
        tp = s["true_positives"]
        fp = s["false_positives"]
        miss = s["missed_mistakes"]

        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + miss) if (tp + miss) > 0 else 0.0

        total_tp += tp
        total_fp += fp
        total_miss += miss

        precision_str = f"{precision:.1%}" if (tp + fp) > 0 else "N/A"
        recall_str = f"{recall:.1%}" if (tp + miss) > 0 else "N/A"

        print(f"{key:<35} | {tp:<4} | {fp:<4} | {miss:<4} | {precision_str:<10} | {recall_str:<10}")

    print("-" * 80)
    overall_prec = total_tp / (total_tp + total_fp) if (total_tp + total_fp) > 0 else 0.0
    overall_rec = total_tp / (total_tp + total_miss) if (total_tp + total_miss) > 0 else 0.0
    overall_prec_str = f"{overall_prec:.1%}" if (total_tp + total_fp) > 0 else "N/A"
    overall_rec_str = f"{overall_rec:.1%}" if (total_tp + total_miss) > 0 else "N/A"

    print(
        f"{'OVERALL TOTALS':<35} | {total_tp:<4} | {total_fp:<4} | {total_miss:<4} | "
        f"{overall_prec_str:<10} | {overall_rec_str:<10}"
    )
    print("=" * 80 + "\n")


async def cmd_simulate(db_path: str, heavy_edit: float, manual_rewrite: float, retry_seconds: float) -> None:
    """Simulates precision and recall outcomes under custom threshold parameters."""
    labeled_mistakes = await get_all_labeled_mistakes(db_path)
    labeled_misses = await get_labeled_missed_mistakes(db_path)

    if not labeled_mistakes:
        print("No labeled mistakes found to run simulation on.")
        return

    print("\n" + "=" * 80)
    print(f"SIMULATION RUN: Proposed Threshold Parameters")
    print(f"  - Heavy Edit Threshold:    {heavy_edit:.2f}")
    print(f"  - Manual Rewrite Threshold: {manual_rewrite:.2f}")
    print(f"  - Immediate Retry Max:     {retry_seconds:.1f}s")
    print("=" * 80)

    # Track metrics
    tp, fp, fn = 0, 0, 0

    for m in labeled_mistakes:
        val = parse_metrics(m["detection_signal"], m["failure_summary"])
        label = m["label"]
        signal = m["detection_signal"].lower()

        # Determine if it would trigger under simulated thresholds
        triggers = True
        if val is not None:
            if signal == "heavy_edit":
                triggers = val > heavy_edit
            elif signal == "manual_rewrite":
                triggers = val > manual_rewrite
            elif signal == "immediate_retry":
                triggers = val <= retry_seconds

        # Evaluate performance
        if triggers:
            if label == "true_mistake":
                tp += 1
            elif label == "false_positive":
                fp += 1
        else:
            if label == "true_mistake":
                # It was a true mistake, but our higher threshold missed it!
                fn += 1

    # Missed mistakes that were never triggered originally remain misses (FN)
    fn += len(labeled_misses)

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0

    print(f"Results of Simulation:")
    print(f"  - True Positives (Triggered Mistakes):      {tp}")
    print(f"  - False Positives (Triggered Non-Mistakes): {fp}")
    print(f"  - False Negatives (Missed Mistakes):        {fn}")
    print(f"  - Precision:                                {precision:.1%}")
    print(f"  - Recall Proxy:                             {recall:.1%}")
    print("=" * 80 + "\n")


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="VexIQ CLI Threshold Parameter Tuning and Simulation Engine."
    )
    parser.add_argument(
        "--db-path",
        default=get_settings().vexiq_db_path,
        help="Database file path override.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # Label command
    subparsers.add_parser("label", help="Interactively label auto-detected mistakes.")

    # Stats command
    subparsers.add_parser("stats", help="Compute metrics on labeled events.")

    # Simulate command
    sim_parser = subparsers.add_parser(
        "simulate", help="Simulate metrics under hypothetical thresholds."
    )
    sim_parser.add_argument(
        "--heavy-edit",
        type=float,
        default=0.30,
        help="Simulated heavy edit threshold (0.0 to 1.0)",
    )
    sim_parser.add_argument(
        "--manual-rewrite",
        type=float,
        default=0.50,
        help="Simulated manual rewrite threshold (0.0 to 1.0)",
    )
    sim_parser.add_argument(
        "--retry-seconds",
        type=float,
        default=120.0,
        help="Simulated immediate retry seconds cutoff",
    )

    args = parser.parse_args()
    db_path = args.db_path

    # Initialize DB (creates directories and schemas if not present)
    await init_db(db_path)

    if args.command == "label":
        await cmd_label(db_path)
    elif args.command == "stats":
        await cmd_stats(db_path)
    elif args.command == "simulate":
        await cmd_simulate(
            db_path,
            heavy_edit=args.heavy_edit,
            manual_rewrite=args.manual_rewrite,
            retry_seconds=args.retry_seconds,
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nSession interrupted.")
        sys.exit(0)
