"""pyflows CLI entry point."""

import json
import logging
import os
import sys
from dataclasses import asdict
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from pyflows import __version__
from pyflows.config import PyflowsConfig, load_config
from pyflows.logging_utils import JsonFormatter, TextFormatter
from pyflows.db import FileDB, FileStatus
from pyflows.pipeline import analyze_changes, analyze_changes_detailed, encode_file, build_encode_command
from pyflows.plan import FilePlan, plan_from_probe
from pyflows.probe import probe_file
from pyflows.scanner import scan_library

console = Console()
log = logging.getLogger("pyflows")


def resolve_config(config_path: Path | None) -> Path:
    if config_path is not None:
        return config_path
    env_path = os.environ.get("PYFLOWS_CONFIG")
    if env_path:
        p = Path(env_path)
        if p.exists():
            return p
    for candidate in [
        Path.home() / ".config" / "pyflows" / "config.yaml",
        Path("/etc/pyflows/config.yaml"),
    ]:
        if candidate.exists():
            return candidate
    console.print("[red]No config file found. Use --config or set PYFLOWS_CONFIG.[/red]")
    sys.exit(1)


def configure_logging(config: PyflowsConfig) -> None:
    level = getattr(logging, config.general.log_level.upper(), logging.INFO)
    handler = logging.FileHandler(config.general.log_output) if config.general.log_output != "stdout" else logging.StreamHandler()

    if config.general.log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(TextFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    root.addHandler(handler)


@click.group()
@click.version_option(version=__version__)
def main() -> None:
    """pyflows — Media library transcoder."""
    pass


@main.command()
@click.option("--config", "config_path", type=click.Path(exists=True, path_type=Path), required=False, default=None)
def run(config_path: Path | None) -> None:
    """Start daemon (scanner + worker + watcher)."""
    config_path = resolve_config(config_path)
    config = load_config(config_path)
    configure_logging(config)
    from pyflows.metrics import start_metrics_server
    try:
        start_metrics_server(config.general.metrics_port, config.general.db_path)
    except OSError as e:
        log.error("Failed to start metrics server on port %d: %s", config.general.metrics_port, e)
        sys.exit(1)
    from pyflows.tasks import start_daemon
    start_daemon(config)


@main.command()
@click.option("--config", "config_path", type=click.Path(exists=True, path_type=Path), required=False, default=None)
def scan(config_path: Path | None) -> None:
    """One-off library scan, queue new files."""
    config_path = resolve_config(config_path)
    config = load_config(config_path)
    configure_logging(config)
    total = 0
    with FileDB(config.general.db_path) as db:
        for lib in config.libraries:
            new_files = scan_library(lib, db)
            console.print(f"  {lib.name}: {len(new_files)} new files")
            total += len(new_files)
    console.print(f"\n[bold]Total queued: {total}[/bold]")


@main.command()
@click.argument("file_path", type=click.Path(exists=True))
@click.option("--profile", required=True)
@click.option("--config", "config_path", type=click.Path(exists=True, path_type=Path), required=False, default=None)
def encode(file_path: str, profile: str, config_path: Path | None) -> None:
    """Process a single file."""
    config_path = resolve_config(config_path)
    config = load_config(config_path)
    configure_logging(config)
    if profile not in config.profiles:
        console.print(f"[red]Unknown profile: {profile}[/red]")
        sys.exit(1)

    from pyflows.pipeline import EncodeStatus
    result = encode_file(
        input_path=file_path,
        profile=config.profiles[profile],
        temp_dir=config.general.temp_dir,
        vaapi_device=config.general.vaapi_device,
        ffmpeg_path=config.general.ffmpeg_path,
        ffprobe_path=config.general.ffprobe_path,
        hardware_config=config.hardware,
        stall_timeout=config.general.stall_timeout,
    )
    if result.status != EncodeStatus.FAILED:
        message = f"encoded -> {result.final_path}" if result.status == EncodeStatus.COMPLETED else "skipped"
        console.print(f"[green]Done:[/green] {message}")
    else:
        console.print(f"[red]Failed:[/red] {result.error}")
        sys.exit(1)


@main.command()
@click.option("--config", "config_path", type=click.Path(exists=True, path_type=Path), required=False, default=None)
def status(config_path: Path | None) -> None:
    """Show queue status."""
    config_path = resolve_config(config_path)
    config = load_config(config_path)

    table = Table(title="pyflows Queue Status")
    table.add_column("Status", style="bold")
    table.add_column("Count", justify="right")

    with FileDB(config.general.db_path) as db:
        for s in [FileStatus.PENDING, FileStatus.PROCESSING, FileStatus.COMPLETED,
                  FileStatus.FAILED, FileStatus.SKIPPED]:
            count = db.count_by_status(s)
            color = {"pending": "yellow", "processing": "cyan", "completed": "green",
                     "failed": "red", "skipped": "dim"}.get(s, "")
            table.add_row(f"[{color}]{s}[/{color}]", str(count))

    console.print(table)


@main.command()
@click.option("--config", "config_path", type=click.Path(exists=True, path_type=Path), required=False, default=None)
@click.option("--limit", default=20, help="Number of records to show")
def history(config_path: Path | None, limit: int) -> None:
    """Show processing history."""
    config_path = resolve_config(config_path)
    config = load_config(config_path)

    table = Table(title="Recent History")
    table.add_column("File", max_width=50)
    table.add_column("Status")
    table.add_column("Codec")
    table.add_column("Completed")

    with FileDB(config.general.db_path) as db:
        for record in db.get_history(limit=limit):
            name = Path(record["path"]).name
            status_val: str = record["status"]
            color = {"completed": "green", "failed": "red", "skipped": "dim"}.get(
                status_val, ""
            )
            table.add_row(
                name,
                f"[{color}]{status_val}[/{color}]",
                record["output_codec"] or "",
                (record["completed_at"] or "")[:19],
            )

    console.print(table)


def render_plan_json(plan: FilePlan) -> None:
    payload = asdict(plan)
    payload["source_probe"] = {
        "video": asdict(plan.source_probe.video) if plan.source_probe.video else None,
        "audio": [asdict(stream) for stream in plan.source_probe.audio],
        "subtitles": [asdict(stream) for stream in plan.source_probe.subtitles],
    }
    console.print(json.dumps(payload, indent=2, ensure_ascii=False))


@main.command()
@click.argument("file_path", type=click.Path(exists=True))
@click.option("--profile", required=True)
@click.option("--config", "config_path", type=click.Path(exists=True, path_type=Path), required=False, default=None)
@click.option("--json", "as_json", is_flag=True, help="Render the full file plan as JSON")
def check(file_path: str, profile: str, config_path: Path | None, as_json: bool) -> None:
    """Dry run — show what would happen to a file."""
    config_path = resolve_config(config_path)
    config = load_config(config_path)
    if profile not in config.profiles:
        console.print(f"[red]Unknown profile: {profile}[/red]")
        sys.exit(1)
    prof = config.profiles[profile]
    vaapi_device = config.general.vaapi_device

    probe = probe_file(file_path, ffprobe_path=config.general.ffprobe_path)

    console.print(f"\n[bold]File:[/bold] {file_path}")
    console.print(f"[bold]Video:[/bold] {probe.video.codec if probe.video else 'none'}")
    console.print(f"[bold]Audio:[/bold] {len(probe.audio)} tracks")
    console.print(f"[bold]Subtitles:[/bold] {len(probe.subtitles)} tracks")

    changes = analyze_changes(probe, prof, file_path)
    detailed = analyze_changes_detailed(probe, prof, file_path)
    plan = plan_from_probe(file_path, probe, prof)

    if as_json:
        render_plan_json(plan)
        return

    if plan.should_skip:
        codec_name = probe.video.codec if probe.video else "unknown"
        console.print(f"\n[yellow]SKIP — already compliant ({codec_name})[/yellow]")
        return

    console.print("\n[bold]Changes needed:[/bold]")
    for key, changed in changes.items():
        console.print(f"  {key}: {'yes' if changed else 'no'}")
        if changed:
            for reason in detailed[key]:
                console.print(f"    - {reason}")

    console.print("\n[bold]Planned output summary:[/bold]")
    console.print(f"  status: {plan.status}")
    console.print(f"  container: {plan.output.target_container}")
    console.print(f"  final path: {plan.output.output_path}")
    console.print(f"  video: {plan.video.action} -> {plan.video.target_codec}")

    console.print("  audio tracks:")
    if plan.audio:
        for audio_item in plan.audio:
            marker = " [default]" if audio_item.default else ""
            console.print(
                f"    - {audio_item.action}: {audio_item.language or 'unknown'} / {audio_item.target_codec.upper()} / {audio_item.target_channels}ch{marker}"
            )
    else:
        console.print("    - none")

    console.print("  subtitle tracks:")
    if plan.subtitles:
        for subtitle_item in plan.subtitles:
            marker = " [default]" if subtitle_item.default else ""
            console.print(
                f"    - {subtitle_item.action}: {subtitle_item.language or 'unknown'} / {(subtitle_item.target_codec or 'unknown').upper()}{marker}"
            )
    else:
        console.print("    - none")

    cmd = build_encode_command(
        file_path,
        "/tmp/output.mkv",
        probe,
        prof,
        vaapi_device,
        ffmpeg_path=config.general.ffmpeg_path,
        hardware_config=config.hardware,
    )
    console.print(f"\n[bold]ffmpeg command:[/bold]")
    console.print(" ".join(cmd.build()))


@main.command()
@click.argument("file_path", type=click.Path(exists=True))
@click.option("--profile", required=True)
@click.option("--config", "config_path", type=click.Path(exists=True, path_type=Path), required=False, default=None)
@click.option("--json", "as_json", is_flag=True, default=False, help="Render the full file plan as JSON")
def plan(file_path: str, profile: str, config_path: Path | None, as_json: bool) -> None:
    """Render the explicit file plan for inspection or machine consumption."""
    config_path = resolve_config(config_path)
    config = load_config(config_path)
    if profile not in config.profiles:
        console.print(f"[red]Unknown profile: {profile}[/red]")
        sys.exit(1)
    probe = probe_file(file_path, ffprobe_path=config.general.ffprobe_path)
    file_plan = plan_from_probe(file_path, probe, config.profiles[profile])

    if as_json:
        render_plan_json(file_plan)
    else:
        console.print(file_plan)


@main.command()
@click.option("--config", "config_path", type=click.Path(exists=True, path_type=Path), required=False, default=None)
@click.option("--file", "file_path", type=str, default=None, help="Retry a specific failed file")
@click.option("--all", "all_failed", is_flag=True, help="Retry all failed files")
def retry(config_path: Path | None, file_path: str | None, all_failed: bool) -> None:
    """Re-queue failed files for processing."""
    config_path = resolve_config(config_path)
    config = load_config(config_path)
    if not file_path and not all_failed:
        console.print("[red]Specify --file <path> or --all[/red]")
        sys.exit(1)
    with FileDB(config.general.db_path) as db:
        if all_failed:
            count = db.retry_all_failed()
            console.print(f"[green]Reset {count} failed files to pending[/green]")
        elif file_path:
            if db.retry_failed(file_path):
                console.print(f"[green]Reset to pending:[/green] {file_path}")
            else:
                console.print(f"[yellow]No failed record found for:[/yellow] {file_path}")


@main.command()
@click.option("--config", "config_path", type=click.Path(exists=True, path_type=Path), required=False, default=None)
@click.option("--processing", is_flag=True, help="Reset stuck processing files to pending")
def reset(config_path: Path | None, processing: bool) -> None:
    """Reset stuck files (crash recovery)."""
    config_path = resolve_config(config_path)
    config = load_config(config_path)
    if not processing:
        console.print("[red]Specify --processing to reset stuck files[/red]")
        sys.exit(1)
    with FileDB(config.general.db_path) as db:
        count = db.reset_processing()
        console.print(f"[green]Reset {count} processing files to pending[/green]")


if __name__ == "__main__":
    main()
