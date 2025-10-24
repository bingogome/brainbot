#!/usr/bin/env python3
"""
Remove incomplete LeRobot recording episodes and rebuild the dataset safely.

This script leans entirely on the LeRobot dataset utilities so that the cleaned
dataset looks exactly like a freshly recorded one—metadata, stats, and video
artifacts are regenerated using the official writers instead of ad-hoc logic.

Typical usage (from repo root):

    python3 brainbot/scripts/prune_incomplete_episodes.py --root xlerobot-data

Use `--dry-run` first to review which episodes would be trimmed.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Iterable

try:
    import pyarrow.dataset as pa_ds
except ImportError as exc:  # pragma: no cover - dependency guard
    raise SystemExit("pyarrow is required. Install it with `pip install pyarrow`.") from exc

import numpy as np
from datasets import Dataset as HFDataset

try:
    from lerobot.datasets.dataset_tools import delete_episodes
    from lerobot.datasets.lerobot_dataset import LeRobotDataset, LeRobotDatasetMetadata
    from lerobot.datasets.utils import (
        load_info,
        load_tasks,
        write_info,
        write_stats,
        write_episodes,
    )
    from lerobot.datasets.video_utils import get_video_duration_in_s
    from lerobot.datasets.compute_stats import aggregate_stats
except ImportError as exc:  # pragma: no cover - dependency guard
    raise SystemExit(
        "LeRobot is required. Ensure the repository dependencies are installed (pip install -e lerobot)."
    ) from exc

try:
    from pyarrow import compute as pc
except ImportError as exc:  # pragma: no cover - dependency guard
    raise SystemExit("pyarrow is required. Install it with `pip install pyarrow`.") from exc


class RunningVideoStats:
    def __init__(self, bins: int = 256):
        self.frame_count = 0
        self.pixel_count = np.zeros(3, dtype=np.float64)
        self.min = np.full(3, np.inf, dtype=np.float64)
        self.max = np.full(3, -np.inf, dtype=np.float64)
        self.sum = np.zeros(3, dtype=np.float64)
        self.sumsq = np.zeros(3, dtype=np.float64)
        self.hist = np.zeros((3, bins), dtype=np.float64)
        self.bin_edges = np.linspace(0.0, 1.0, bins + 1)

    def update(self, frame: np.ndarray) -> None:
        if frame.ndim != 3 or frame.shape[2] != 3:
            raise ValueError("Expected RGB frame with shape (H, W, 3)")
        arr = frame.reshape(-1, 3).astype(np.float32) / 255.0
        self.frame_count += 1
        counts = arr.shape[0]
        self.pixel_count += counts
        self.min = np.minimum(self.min, arr.min(axis=0))
        self.max = np.maximum(self.max, arr.max(axis=0))
        self.sum += arr.sum(axis=0)
        self.sumsq += np.square(arr).sum(axis=0)
        for channel in range(3):
            hist, _ = np.histogram(arr[:, channel], bins=self.bin_edges)
            self.hist[channel] += hist

    def finalize(self) -> dict[str, np.ndarray]:
        if self.frame_count == 0:
            return {
                "min": np.zeros((3, 1, 1), dtype=np.float64),
                "max": np.zeros((3, 1, 1), dtype=np.float64),
                "mean": np.zeros((3, 1, 1), dtype=np.float64),
                "std": np.zeros((3, 1, 1), dtype=np.float64),
                "count": np.array([0], dtype=np.int64),
                "q01": np.zeros((3, 1, 1), dtype=np.float64),
                "q10": np.zeros((3, 1, 1), dtype=np.float64),
                "q50": np.zeros((3, 1, 1), dtype=np.float64),
                "q90": np.zeros((3, 1, 1), dtype=np.float64),
                "q99": np.zeros((3, 1, 1), dtype=np.float64),
            }

        mean = self.sum / self.pixel_count
        variance = np.maximum(self.sumsq / self.pixel_count - np.square(mean), 0.0)
        std = np.sqrt(variance)

        quantiles = {}
        quantile_targets = {
            "q01": 0.01,
            "q10": 0.10,
            "q50": 0.50,
            "q90": 0.90,
            "q99": 0.99,
        }
        cumulative_edges = (self.bin_edges[:-1] + self.bin_edges[1:]) / 2.0
        for name, target in quantile_targets.items():
            values = []
            for channel in range(3):
                total = self.hist[channel].sum()
                if total == 0:
                    values.append(mean[channel])
                    continue
                cumsum = np.cumsum(self.hist[channel])
                idx = np.searchsorted(cumsum, target * total, side="left")
                idx = min(idx, len(cumsum) - 1)
                values.append(float(cumulative_edges[idx]))
            quantiles[name] = np.array(values, dtype=np.float64)

        reshape = lambda arr: arr.reshape(3, 1, 1)

        return {
            "min": reshape(self.min),
            "max": reshape(self.max),
            "mean": reshape(mean),
            "std": reshape(std),
            "count": np.array([self.frame_count], dtype=np.int64),
            "q01": reshape(quantiles["q01"]),
            "q10": reshape(quantiles["q10"]),
            "q50": reshape(quantiles["q50"]),
            "q90": reshape(quantiles["q90"]),
            "q99": reshape(quantiles["q99"]),
        }


class EpisodeAggregate:
    def __init__(self, episode_index: int, features: dict, fps: float):
        self.episode_index = episode_index
        self.features = features
        self.fps = fps
        self.length = 0
        self.data_chunk_index: int | None = None
        self.data_file_index: int | None = None
        self.dataset_from_index: int | None = None
        self.dataset_to_index: int | None = None
        self.timestamps: list[float] = []
        self.indices: list[int] = []
        self.task_indices: list[int] = []
        self.numeric_keys = [
            key for key, meta in features.items() if meta["dtype"] not in {"video", "image"}
        ]
        self.samples: dict[str, list[np.ndarray]] = {key: [] for key in self.numeric_keys}
        self.video_stats: dict[str, RunningVideoStats] = {}
        self.video_ranges: dict[str, tuple[float, float]] = {}
        self.video_files: dict[str, tuple[int, int]] = {}

    def _to_array(self, value, expected_size: int) -> np.ndarray:
        arr = np.asarray(value)
        if arr.ndim == 0:
            arr = arr.reshape(1)
        return arr.reshape(-1) if expected_size else arr

    def add_row(self, row: dict, chunk_idx: int, file_idx: int) -> None:
        if self.data_chunk_index is None:
            self.data_chunk_index = chunk_idx
        elif self.data_chunk_index != chunk_idx:
            raise ValueError(f"Episode {self.episode_index} spans multiple data chunks")

        if self.data_file_index is None:
            self.data_file_index = file_idx
        elif self.data_file_index != file_idx:
            raise ValueError(f"Episode {self.episode_index} spans multiple data files")

        self.length += 1

        index_val = int(np.asarray(row["index"]).item())
        self.indices.append(index_val)
        if self.dataset_from_index is None:
            self.dataset_from_index = index_val
            self.dataset_to_index = index_val
        else:
            self.dataset_from_index = min(self.dataset_from_index, index_val)
            self.dataset_to_index = max(self.dataset_to_index or index_val, index_val)

        task_idx = int(np.asarray(row["task_index"]).item())
        self.task_indices.append(task_idx)

        if "timestamp" in row:
            timestamp_val = float(np.asarray(row["timestamp"]).item())
        else:
            timestamp_val = index_val / self.fps
        self.timestamps.append(timestamp_val)

        for key in self.numeric_keys:
            meta = self.features[key]
            shape = meta.get("shape") or []
            size = int(np.prod(shape)) if shape else len(np.atleast_1d(row[key]))
            arr = self._to_array(row[key], size).astype(np.float64)
            self.samples[key].append(arr)

    def prepare_ranges(self, video_keys: list[str]) -> None:
        if not self.timestamps:
            start = self.dataset_from_index / self.fps if self.dataset_from_index is not None else 0.0
        else:
            start = float(self.timestamps[0])
        end = start + self.length / self.fps
        for key in video_keys:
            self.video_ranges[key] = (start, end)
            if key not in self.video_stats:
                self.video_stats[key] = RunningVideoStats()

    def set_video_file(self, video_key: str, chunk_idx: int, file_idx: int) -> None:
        self.video_files[video_key] = (chunk_idx, file_idx)

    def update_video_stats(self, video_key: str, frame) -> None:
        frame_array = frame.to_ndarray(format="rgb24")
        self.video_stats[video_key].update(frame_array)

    def get_video_stats(self, video_key: str) -> dict[str, np.ndarray]:
        return self.video_stats[video_key].finalize()

    def _reshape_stat(self, array: np.ndarray, shape: list[int]) -> np.ndarray:
        if not shape:
            return array.reshape(1)
        return array.reshape(tuple(shape))

    def finalize(self, task_map: dict[int, str], video_keys: list[str]) -> tuple[dict, dict]:
        if self.dataset_from_index is None:
            raise ValueError("Episode has no frames")
        self.dataset_to_index = (self.dataset_to_index or 0) + 1

        tasks = sorted({task_map.get(idx, f"task_{idx}") for idx in self.task_indices})

        episode_dict = {
            "episode_index": [self.episode_index],
            "tasks": [tasks],
            "length": [self.length],
            "data/chunk_index": [self.data_chunk_index or 0],
            "data/file_index": [self.data_file_index or 0],
            "dataset_from_index": [self.dataset_from_index],
            "dataset_to_index": [self.dataset_to_index],
            "meta/episodes/chunk_index": [0],
            "meta/episodes/file_index": [0],
        }

        episode_stats: dict[str, dict[str, np.ndarray]] = {}
        quantiles = {
            "q01": 0.01,
            "q10": 0.10,
            "q50": 0.50,
            "q90": 0.90,
            "q99": 0.99,
        }

        for key in self.numeric_keys:
            samples = self.samples[key]
            if not samples:
                continue
            stack = np.stack(samples, axis=0)
            shape = self.features[key].get("shape") or [stack.shape[-1]]
            size = int(np.prod(shape)) if shape else 1
            stack = stack.reshape(self.length, size)

            min_flat = stack.min(axis=0)
            max_flat = stack.max(axis=0)
            mean_flat = stack.mean(axis=0)
            std_flat = stack.std(axis=0)

            stats_arrays = {
                "min": min_flat,
                "max": max_flat,
                "mean": mean_flat,
                "std": std_flat,
                "count": np.array([self.length], dtype=np.int64),
            }
            for name, q in quantiles.items():
                stats_arrays[name] = np.quantile(stack, q, axis=0)

            episode_stats[key] = {}
            for name, arr in stats_arrays.items():
                if name == "count":
                    episode_stats[key][name] = arr.astype(np.int64)
                    episode_dict[f"stats/{key}/{name}"] = [arr.tolist()]
                else:
                    reshaped = self._reshape_stat(arr, shape)
                    episode_stats[key][name] = reshaped
                    episode_dict[f"stats/{key}/{name}"] = [reshaped.tolist()]

        for key in video_keys:
            chunk_idx, file_idx = self.video_files.get(key, (0, 0))
            start, end = self.video_ranges.get(key, (0.0, 0.0))
            episode_dict[f"videos/{key}/chunk_index"] = [chunk_idx]
            episode_dict[f"videos/{key}/file_index"] = [file_idx]
            episode_dict[f"videos/{key}/from_timestamp"] = [start]
            episode_dict[f"videos/{key}/to_timestamp"] = [end]
            if key in self.video_stats:
                video_stat_arrays = self.get_video_stats(key)
                episode_stats[key] = {}
                for name, arr in video_stat_arrays.items():
                    if name == "count":
                        episode_stats[key][name] = arr.astype(np.int64)
                        episode_dict[f"stats/{key}/{name}"] = [arr.tolist()]
                    else:
                        episode_stats[key][name] = arr
                        episode_dict[f"stats/{key}/{name}"] = [arr.tolist()]

        return episode_dict, episode_stats


def find_incomplete_episodes(dataset_dir: Path, tolerance_s: float) -> list[int]:
    """Return the episode indices that must be dropped to restore a consistent dataset."""
    metadata = LeRobotDatasetMetadata(repo_id=dataset_dir.name, root=dataset_dir)
    episodes_ds = metadata.episodes if metadata.episodes is not None else load_episodes(metadata.root)
    total_episodes = len(episodes_ds) if episodes_ds is not None else 0

    data_dir = dataset_dir / "data"
    if not data_dir.is_dir() or total_episodes == 0:
        return []

    data_dataset = pa_ds.dataset(str(data_dir), format="parquet")

    drop_candidates: set[int] = set()
    video_segments: defaultdict[tuple[str, int, int], list[tuple[int, float, float]]] = defaultdict(list)

    for ep_idx in range(total_episodes):
        record = episodes_ds[ep_idx]
        expected_len = int(record.get("length", 0))
        actual_len = data_dataset.count_rows(filter=pa_ds.field("episode_index") == ep_idx)

        if actual_len != expected_len:
            logging.warning(
                "  • episode %d data length mismatch (expected %d rows, found %d)",
                ep_idx,
                expected_len,
                actual_len,
            )
            drop_candidates.add(ep_idx)

        for key in metadata.video_keys:
            chunk_idx = record.get(f"videos/{key}/chunk_index")
            file_idx = record.get(f"videos/{key}/file_index")
            if chunk_idx is None or file_idx is None:
                continue
            from_ts = float(record.get(f"videos/{key}/from_timestamp", 0.0))
            to_ts = float(record.get(f"videos/{key}/to_timestamp", 0.0))
            video_segments[(key, chunk_idx, file_idx)].append((ep_idx, from_ts, to_ts))

    if metadata.video_path:
        for (key, chunk_idx, file_idx), entries in video_segments.items():
            video_path = dataset_dir / metadata.video_path.format(
                video_key=key, chunk_index=chunk_idx, file_index=file_idx
            )
            if not video_path.exists():
                logging.warning(
                    "  • video file missing for %s chunk=%03d file=%03d; dropping associated episodes",
                    key,
                    chunk_idx,
                    file_idx,
                )
                drop_candidates.update(ep_idx for ep_idx, _, _ in entries)
                continue

            try:
                duration = get_video_duration_in_s(video_path)
            except Exception as exc:  # pragma: no cover - defensive
                logging.warning("  • failed to read duration of %s (%s); dropping associated episodes", video_path, exc)
                drop_candidates.update(ep_idx for ep_idx, _, _ in entries)
                continue

            for ep_idx, _, to_ts in entries:
                if duration + tolerance_s < to_ts:
                    logging.warning(
                        "  • episode %d references %s beyond video duration (%.2fs < %.2fs)",
                        ep_idx,
                        video_path.name,
                        duration,
                        to_ts,
                    )
                    drop_candidates.add(ep_idx)

    if not drop_candidates:
        return []

    first_bad = min(drop_candidates)
    drop_candidates.update(range(first_bad, total_episodes))
    return sorted(drop_candidates)


def metadata_needs_rebuild(dataset_dir: Path) -> bool:
    episodes_dir = dataset_dir / "meta" / "episodes"
    if not episodes_dir.exists():
        return True
    parquet_files = list(episodes_dir.rglob("file-*.parquet"))
    if not parquet_files:
        return True
    try:
        sample_path = parquet_files[0]
        import pyarrow.parquet as pq  # type: ignore
        pq.read_table(sample_path, columns=["episode_index"])
    except Exception:
        return True
    return False


def rebuild_metadata(dataset_dir: Path) -> None:
    logging.info("  • rebuilding metadata from raw parquet and videos")
    info = load_info(dataset_dir)
    fps = float(info.get("fps", 30.0))
    features = info["features"]
    video_keys = [key for key, meta in features.items() if meta["dtype"] == "video"]

    tasks_df = load_tasks(dataset_dir)
    task_map: dict[int, str] = {}
    if tasks_df is not None:
        tasks_df = tasks_df.reset_index().rename(columns={"index": "task"})
        for _, row in tasks_df.iterrows():
            task_map[int(row["task_index"])] = row["task"]

    data_dir = dataset_dir / "data"
    dataset = pa_ds.dataset(str(data_dir), format="parquet")
    episodes: dict[int, EpisodeAggregate] = {}

    feature_columns = list(features.keys())
    for fragment in dataset.get_fragments():
        fragment_path = Path(fragment.path)
        try:
            chunk_idx = int(fragment_path.parent.name.split("-")[1])
            file_idx = int(fragment_path.name.split("-")[1].split(".")[0])
        except Exception as exc:
            raise ValueError(f"Unexpected data file layout: {fragment.path}") from exc

        table = fragment.to_table(columns=feature_columns)
        data = table.to_pydict()
        length = len(table)
        for i in range(length):
            ep_idx = int(np.asarray(data["episode_index"][i]).item())
            episode = episodes.setdefault(ep_idx, EpisodeAggregate(ep_idx, features, fps))
            row = {key: data[key][i] for key in feature_columns}
            # ensure scalar fields accessible
            row["index"] = data["index"][i]
            row["task_index"] = data["task_index"][i]
            if "timestamp" in row:
                row["timestamp"] = row["timestamp"]
            episode.add_row(row, chunk_idx, file_idx)

    if not episodes:
        raise ValueError("No frames found while rebuilding metadata")

    sorted_eps = sorted(episodes.keys())

    for ep_idx in sorted_eps:
        episodes[ep_idx].prepare_ranges(video_keys)

    for key in video_keys:
        video_dir = dataset_dir / "videos" / key
        mp4_files = sorted(video_dir.glob("chunk-*/file-*.mp4"))
        if not mp4_files:
            logging.warning("  • video key '%s' missing files; stats will be zeroed", key)
            for ep in episodes.values():
                ep.set_video_file(key, 0, 0)
            continue
        path = mp4_files[0]
        chunk_idx = int(path.parent.name.split("-")[1])
        file_idx = int(path.name.split("-")[1].split(".")[0])
        for ep in episodes.values():
            ep.set_video_file(key, chunk_idx, file_idx)
        try:
            import av
        except ImportError as exc:
            logging.warning("  • PyAV missing, skipping video stats for '%s' (%s)", key, exc)
            continue

        episodes_seq = [episodes[idx] for idx in sorted_eps]
        tolerance = 1.0 / fps
        container = av.open(str(path))
        stream = container.streams.video[0]
        time_base = stream.time_base
        current = 0
        max_index = len(episodes_seq) - 1
        for frame in container.decode(stream):
            pts = frame.pts if frame.pts is not None else None
            timestamp = float(pts * time_base) if pts is not None else None
            if timestamp is None:
                timestamp = episodes_seq[current].video_ranges[key][0] + (
                    episodes_seq[current].video_stats[key].frame_count / fps
                )
            while current < max_index and timestamp > episodes_seq[current].video_ranges[key][1] + tolerance:
                current += 1
            if current > max_index:
                break
            start, end = episodes_seq[current].video_ranges[key]
            if start - tolerance <= timestamp <= end + tolerance:
                episodes_seq[current].update_video_stats(key, frame)
        container.close()

    episode_dicts = []
    stats_list = []
    for ep_idx in sorted_eps:
        episode_dict, stats = episodes[ep_idx].finalize(task_map, video_keys)
        episode_dicts.append(episode_dict)
        stats_list.append(stats)

    episodes_dir = dataset_dir / "meta" / "episodes"
    if episodes_dir.exists():
        shutil.rmtree(episodes_dir)
    episodes_dir.mkdir(parents=True, exist_ok=True)

    hf_dataset = HFDataset.from_list(episode_dicts)
    write_episodes(hf_dataset, dataset_dir)

    global_stats = aggregate_stats(stats_list)
    write_stats(global_stats, dataset_dir)

    info["total_episodes"] = len(sorted_eps)
    info["total_frames"] = sum(ep["length"][0] for ep in episode_dicts)
    info["splits"] = {"train": f"0:{len(sorted_eps)}"}
    if task_map:
        info["total_tasks"] = len(task_map)
    write_info(info, dataset_dir)


def move_to_failed(dataset_dir: Path, root: Path) -> None:
    failed_dir = root / "failed_datasets"
    failed_dir.mkdir(exist_ok=True)
    target = failed_dir / dataset_dir.name
    if target.exists():
        shutil.rmtree(target)
    logging.info("  • moving dataset to %s", target)
    shutil.move(str(dataset_dir), str(target))


def clean_dataset(
    dataset_dir: Path,
    *,
    tolerance_s: float,
    dry_run: bool,
    keep_backup: bool,
    root: Path,
) -> None:
    logging.info("Processing dataset: %s", dataset_dir.name)
    needs_rebuild = metadata_needs_rebuild(dataset_dir)
    if needs_rebuild and dry_run:
        logging.info("  • metadata missing/corrupted; rerun without --dry-run to rebuild")
        return
    if needs_rebuild:
        try:
            rebuild_metadata(dataset_dir)
        except Exception as exc:
            logging.error("  ! failed to rebuild metadata for %s: %s", dataset_dir.name, exc)
            if not dry_run:
                move_to_failed(dataset_dir, root)
            return
    try:
        drop_eps = find_incomplete_episodes(dataset_dir, tolerance_s)
    except Exception as exc:
        logging.error("  ! failed to inspect dataset %s: %s", dataset_dir.name, exc)
        if not dry_run:
            move_to_failed(dataset_dir, root)
        return

    if not drop_eps:
        logging.info("  ✓ no inconsistencies detected")
        return

    logging.info("  • episodes to remove: %s", ", ".join(str(ep) for ep in drop_eps))
    if dry_run:
        logging.info("  ↪ dry-run enabled; skipping rewrite")
        return

    dataset = LeRobotDataset(repo_id=dataset_dir.name, root=dataset_dir, download_videos=True)

    tmp_root = Path(tempfile.mkdtemp(prefix=f"{dataset_dir.name}_clean_", dir=dataset_dir.parent))
    logging.info("  • rebuilding dataset via LeRobot utilities")
    delete_episodes(dataset, drop_eps, output_dir=tmp_root, repo_id=dataset.repo_id)
    del dataset

    clean_root = tmp_root
    backup_dir = dataset_dir.with_name(dataset_dir.name + ".backup")
    if backup_dir.exists():
        shutil.rmtree(backup_dir)

    try:
        dataset_dir.rename(backup_dir)
        shutil.move(str(clean_root), str(dataset_dir))
    except Exception as exc:  # pragma: no cover - defensive
        logging.error("  ! failed to swap cleaned dataset: %s", exc)
        if dataset_dir.exists():
            shutil.rmtree(dataset_dir)
        backup_dir.rename(dataset_dir)
        move_to_failed(dataset_dir, root)
        raise
    finally:
        if clean_root.exists():
            shutil.rmtree(clean_root, ignore_errors=True)

    if keep_backup:
        logging.info("  • backup retained at %s", backup_dir)
    else:
        shutil.rmtree(backup_dir, ignore_errors=True)
    refreshed_meta = LeRobotDatasetMetadata(repo_id=dataset_dir.name, root=dataset_dir)
    logging.info("  ✓ dataset now contains %d episode(s)", refreshed_meta.total_episodes)


def iter_datasets(root: Path, selection: Iterable[str] | None) -> Iterable[Path]:
    if selection:
        for name in selection:
            candidate = root / name
            if candidate.is_dir():
                yield candidate
            else:
                logging.warning("Dataset '%s' not found under %s", name, root)
        return
    for child in sorted(root.iterdir()):
        if child.is_dir():
            yield child


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Trim incomplete LeRobot episodes and rebuild datasets.")
    parser.add_argument("--root", type=Path, default=Path("xlerobot-data"), help="Directory containing dataset folders.")
    parser.add_argument(
        "--dataset",
        nargs="*",
        help="Specific dataset names to process (defaults to all directories under --root).",
    )
    parser.add_argument("--dry-run", action="store_true", help="Analyse and report issues without rewriting datasets.")
    parser.add_argument(
        "--keep-backup",
        action="store_true",
        help="Keep a copy of the original dataset alongside the cleaned version.",
    )
    parser.add_argument(
        "--video-tolerance",
        type=float,
        default=1.0 / 30.0,
        help="Extra seconds allowed beyond the recorded video duration before an episode is considered truncated.",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(format="[%(levelname)s] %(message)s", level=log_level)

    root = args.root.resolve()
    if not root.is_dir():
        raise SystemExit(f"Dataset root '{root}' does not exist.")

    for dataset_dir in iter_datasets(root, args.dataset):
        try:
            clean_dataset(
                dataset_dir,
                tolerance_s=args.video_tolerance,
                dry_run=args.dry_run,
                keep_backup=args.keep_backup,
                root=root,
            )
        except Exception as exc:
            logging.error("Failed to clean %s: %s", dataset_dir.name, exc)
            if args.dry_run:
                continue
            raise


if __name__ == "__main__":
    main()
