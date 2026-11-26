"""Command line interface."""
import csv
import datetime as dt
import logging
import sys
from collections.abc import Iterable
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Self

import typer
from rich.console import Console
from rich.filesize import decimal
from rich.logging import RichHandler
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeRemainingColumn,
)
from rich.table import Table
from sqlmodel import Session as SQLModelSession

from . import __version__, config
from .api import ProcareClient
from .db import session_scope
from .errors import AuthError, ProcareError
from .models import Kid, MediaItem, MediaKind
from .sync import Syncer, SyncHooks, SyncStats

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
)
console = Console()
error_console = Console(stderr=True)


@dataclass(slots=True)
class ContextObject:
    """
    Per-run state shared between the root callback and the subcommand.

    ``--out`` is a root option, so it is parsed before Typer knows which
    subcommand will run. The catalog is therefore opened on first use rather
    than in the callback; otherwise commands that never touch it, like
    ``login`` or ``version``, would create an output directory and an empty
    catalog wherever they were run.

    It is a context manager so the callback can register it with Typer once,
    and whatever it opened later is closed along with it.
    """

    output_dir: Path
    _resources: ExitStack = field(default_factory=ExitStack)
    _session: SQLModelSession | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._resources.close()

    @property
    def session(self) -> SQLModelSession:
        """The catalog session singleton, opened and migrated on first access."""
        if self._session is None:
            self._session = self._resources.enter_context(session_scope(self.output_dir))
        return self._session


@app.callback()
def _configure(
    ctx: typer.Context,
    output_dir: Annotated[
        Path,
        typer.Option("--out", "-o", help="Output directory."),
    ] = Path("photos"),
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Debug logging to stderr."),
    ] = False,
) -> None:
    """Bulk download your children's photos from Procare Connect."""
    output_dir = output_dir.expanduser().resolve()
    ctx.obj = ctx.with_resource(ContextObject(output_dir))
    logging.basicConfig(
        level=logging.WARNING,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=error_console, show_path=False, rich_tracebacks=True)],
    )
    if verbose:
        logging.getLogger(__package__).setLevel(logging.DEBUG)


def _resolve_credentials(email: str | None, password: str | None) -> config.Credentials:
    """Resolve credentials from arguments, environment values, or a prompt."""
    credentials = config.Credentials.resolve(email, password)
    if not credentials.email:
        credentials.email = typer.prompt("Procare email")
    if not credentials.password:
        credentials.password = typer.prompt("Procare password", hide_input=True)
    return credentials


def _client(rate: float = 1.5) -> ProcareClient:
    """Build a client."""
    token = config.Token.load()
    credentials = config.Credentials()
    if not token and not credentials.complete:
        error_console.print(
            "[red]Not logged in.[/] Run [bold]procare-photos login[/] "
            "or set PROCARE_EMAIL and PROCARE_PASSWORD."
        )
        raise typer.Exit(code=2)
    client = ProcareClient(token=token, credentials=credentials, requests_per_second=rate)
    if not token:
        token = client.login(credentials)
        token.save()
    return client


def _select_kids(kids: list[Kid], terms: list[str]) -> list[Kid]:
    """Filter children by ID or by case-insensitive name fragment."""
    if not terms:
        return kids
    found = []
    for kid in kids:
        for term in terms:
            if kid.id == term or term.lower() in kid.name.lower():
                found.append(kid)
                break
    return found


def _date(value: str | None) -> dt.date | None:
    """A ``YYYY-MM-DD`` option as a date, or a readable complaint if it is not one.

    Absent is not a default here. It means the sweep picks its own bound, which
    it does per child from the record creation date.
    """
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        error_console.print(f"[red]Invalid date '{value}'.[/] Use YYYY-MM-DD.")
        raise typer.Exit(code=2) from None


@dataclass(slots=True)
class KidStats:
    """One child's totals for one kind of media."""

    kid_name: str
    kind: str
    known: int = 0
    downloaded: int = 0
    failed: int = 0
    bytes: int = 0
    earliest: dt.datetime | None = None
    latest: dt.datetime | None = None


# "activitys" is not a word. Pluralising for a progress line is presentation,
# so the labels live here rather than being contorted into the stored value.
KIND_LABELS = {
    MediaKind.PHOTO: "photos",
    MediaKind.VIDEO: "videos",
    MediaKind.ACTIVITY: "activity photos",
}


def _scanning(kids: list[Kid], since: dt.date | None, until: dt.date | None) -> str:
    """What the run is about to cover, in terms of what was actually asked for.

    With no dates there is no single range to name: each child is swept back to
    their own record, and those differ by years.

    The children go on their own lines. Procare reports a name as "Last,
    First", so a comma-separated list of them reads as twice as many people.
    """
    match (since, until):
        case (None, None):
            scope = ""
        case (start, None):
            scope = f" from [bold]{start}[/]"
        case (None, end):
            scope = f" up to [bold]{end}[/]"
        case (start, end):
            scope = f" from [bold]{start}[/] to [bold]{end}[/]"
    names = "\n".join(f"  {kid.name}" for kid in kids)
    return f"Scanning{scope}:\n{names}"


def _day(when: dt.datetime | None) -> str:
    """A timestamp as a bare date, for a report that has no room for the time."""
    return when.date().isoformat() if when else ""


@app.command()
def login(
    email: Annotated[
        str | None,
        typer.Option(help="Procare account email."),
    ] = None,
    password: Annotated[
        str | None,
        typer.Option(help="Procare password. Prompted for if omitted."),
    ] = None,
) -> None:
    """Authenticate and cache an auth token."""
    credentials = _resolve_credentials(email, password)
    with ProcareClient() as client:
        try:
            token = client.login(credentials)
        except AuthError as exc:
            error_console.print(f"[red]Login failed:[/] {exc}")
            raise typer.Exit(code=1) from None
        token.save()
    console.print(f"[green]Logged in as {token.email}.[/]")
    console.print(f"Token cached at {config.Token.path()}")


@app.command()
def logout() -> None:
    """Delete the cached auth token."""
    config.Token.clear()
    console.print("Cached token removed.")


@app.command()
def kids() -> None:
    """List the children attached to this account."""
    with _client() as client:
        _kids_report(client.kids())


def _kids_report(children: Iterable[Kid]) -> None:
    """Print the children on the account."""
    table = Table(title="Children")
    table.add_column("ID")
    table.add_column("Name")
    for kid in children:
        table.add_row(kid.id, kid.name)
    console.print(table)


@app.command()
def sync(
    ctx: typer.Context,
    kid: Annotated[
        list[str] | None,
        typer.Option("--kid", "-k", help="Limit to a child (name or id). Repeatable."),
    ] = None,
    since: Annotated[
        str | None,
        typer.Option(help="Earliest date to scan, YYYY-MM-DD."),
    ] = None,
    until: Annotated[
        str | None,
        typer.Option(help="Latest date to scan, YYYY-MM-DD."),
    ] = None,
    photos: Annotated[
        bool,
        typer.Option(help="Fetch photos."),
    ] = True,
    videos: Annotated[
        bool,
        typer.Option(help="Fetch videos."),
    ] = True,
    activity_photos: Annotated[
        bool,
        typer.Option(help="Fetch photos attached to timeline activities."),
    ] = True,
    concurrency: Annotated[
        int,
        typer.Option(help="Parallel downloads."),
    ] = 8,
    rate: Annotated[
        float,
        typer.Option(help="Max requests per second."),
    ] = 1.5,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="List what is available without downloading."),
    ] = False,
) -> None:
    """Download every photo for each child."""
    since_date = _date(since)
    until_date = _date(until)
    if since_date and until_date and since_date > until_date:
        error_console.print("[red]--since must not be after --until.[/]")
        raise typer.Exit(code=2)

    kinds: list[MediaKind] = []
    if photos:
        kinds.append(MediaKind.PHOTO)
    if videos:
        kinds.append(MediaKind.VIDEO)
    if activity_photos:
        kinds.append(MediaKind.ACTIVITY)

    with _client(rate) as client:
        try:
            kids = client.kids()
        except ProcareError as exc:
            error_console.print(f"[red]{exc}[/]")
            raise typer.Exit(code=1) from None
        if kids:
            ctx.obj.session.exec(Kid.upsert(), params=[kid.model_dump() for kid in kids])
            ctx.obj.session.commit()
        selected_kids = _select_kids(kids, kid or [])
        if not selected_kids:
            console.print(f"[yellow]No child matches {kid}.[/]")
            return

        console.print(_scanning(selected_kids, since_date, until_date))
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeRemainingColumn(),
            console=console,
        ) as progress:
            syncer = Syncer(
                client,
                ctx.obj.session,
                ctx.obj.output_dir,
                concurrency=concurrency,
                hooks=_sync_hooks(progress),
            )
            stats = syncer.run(
                selected_kids,
                kinds,
                since_date,
                until_date,
                dry_run=dry_run,
            )

    _sync_report(stats, dry_run)


def _sync_hooks(progress: Progress) -> SyncHooks:
    """Wire the sync callbacks to a live progress display.

    Scanning cannot be counted towards a total: how many windows a sweep takes
    is only known once it has finished. So it runs as a spinner, and the bar
    with a count on it is added later, once the download queue has been sized.
    """
    scan_task = progress.add_task("Scanning date windows", total=None)
    download_task: TaskID | None = None

    def on_window(kid: Kid, kind: MediaKind, start: dt.date, end: dt.date, total: int) -> None:
        progress.update(
            scan_task,
            description=f"Scanning {kid.name} {KIND_LABELS[kind]} {start}..{end}",
            advance=1,
        )

    def on_discovered(kid: Kid, kind: MediaKind, found: int) -> None:
        progress.console.print(f"  {kid.name}: found {found} {KIND_LABELS[kind]}")

    def on_download_start(total: int) -> None:
        nonlocal download_task
        progress.update(scan_task, description="Scan complete", completed=1, total=1)
        if total:
            download_task = progress.add_task("Downloading", total=total)

    def on_download_done() -> None:
        if download_task is not None:
            progress.advance(download_task)

    def on_download_failed(item: MediaItem, error: str) -> None:
        progress.console.print(f"  [red]failed[/] {item.id}: {error}")

    return SyncHooks(
        on_window=on_window,
        on_discovered=on_discovered,
        on_download_start=on_download_start,
        on_download_done=on_download_done,
        on_download_failed=on_download_failed,
    )


def _sync_report(stats: SyncStats, dry_run: bool) -> None:
    """Print what the run did and whatever it could not fetch."""
    table = Table(title="Sync summary")
    table.add_column("")
    table.add_column("")
    table.add_row("Discovered", str(stats.discovered))
    table.add_row("Already present", str(stats.already_present))
    if stats.downloaded:
        table.add_row("Downloaded", str(stats.downloaded))
    if stats.adopted:
        table.add_row("Adopted", str(stats.adopted))
    if stats.bytes_written:
        table.add_row("Bytes written", decimal(stats.bytes_written))
    if stats.failed:
        table.add_row("[red]Failed[/]", str(stats.failed))
    if stats.skipped_unavailable:
        table.add_row("[yellow]Skipped unavailable[/]", str(stats.skipped_unavailable))
    console.print(table)

    if dry_run:
        console.print("[yellow]Dry run: nothing was downloaded.[/]")


@app.command()
def verify(ctx: typer.Context) -> None:
    """Re-queue catalog entries whose files are missing from disk."""
    requeued = 0
    for item in ctx.obj.session.exec(MediaItem.find_downloaded()):
        if not item.local_path or not (ctx.obj.output_dir / item.local_path).is_file():
            item.local_path = None
            item.size_bytes = None
            item.downloaded_at = None
            item.attempts = 0
            requeued += 1
    ctx.obj.session.commit()
    console.print(f"Re-queued {requeued} item(s) missing from disk.")
    if requeued:
        console.print("Run [bold]sync[/] to fetch them again.")


@app.command(name="reset-failed")
def reset_failed(ctx: typer.Context) -> None:
    """Clear the failure count on items that were given up on."""
    reset = ctx.obj.session.exec(MediaItem.reset_attempts()).rowcount
    ctx.obj.session.commit()
    console.print(f"Reset the failure count on {reset} item(s).")
    if reset:
        console.print("Run [bold]sync[/] to try them again.")


@app.command()
def status(ctx: typer.Context) -> None:
    """Show what the local catalog contains."""
    groups: dict[tuple[str, str], KidStats] = {}
    for item in ctx.obj.session.exec(MediaItem.iter_all()):
        key = (item.kid.name, item.kind)
        if key not in groups:
            groups[key] = KidStats(kid_name=item.kid.name, kind=item.kind)
        totals = groups[key]
        totals.known += 1
        if item.downloaded_at:
            totals.downloaded += 1
        if not item.downloaded_at and item.attempts:
            totals.failed += 1
        totals.bytes += item.size_bytes or 0
        if totals.earliest is None or item.captured_at < totals.earliest:
            totals.earliest = item.captured_at
        if totals.latest is None or item.captured_at > totals.latest:
            totals.latest = item.captured_at
    stats = sorted(groups.values(), key=lambda totals: (totals.kid_name, totals.kind))
    _status_report(stats)


def _status_report(rows: Iterable[KidStats]) -> None:
    """Print the per child totals and the span each one covers.

    Downloaded and known read better as one cell than as two, and the earliest
    and latest dates sit on the row they describe. That keeps the report to a
    single pass, which is what lets the rows arrive as any iterable.

    The seven columns want about 90 columns of terminal. Narrower than that and
    the child names and the dates both start to truncate.
    """
    table = Table(title="Catalog")
    table.add_column("Child")
    table.add_column("Kind")
    table.add_column("Downloaded", justify="right")
    table.add_column("Failed", justify="right")
    table.add_column("Size", justify="right")
    table.add_column("Earliest", justify="right")
    table.add_column("Latest", justify="right")
    for row in rows:
        table.add_row(
            row.kid_name,
            row.kind,
            f"{row.downloaded} / {row.known}",
            str(row.failed),
            decimal(row.bytes),
            _day(row.earliest),
            _day(row.latest),
        )
    console.print(table)


@app.command()
def export(
    ctx: typer.Context,
    dest: Annotated[
        Path,
        typer.Option("--to", help="Destination CSV file."),
    ] = Path("procare-media.csv"),
) -> None:
    """Export the catalog, including captions, to CSV."""
    count = 0
    with open(dest, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=[
            "id",
            "kid_name",
            "kind",
            "captured_at",
            "caption",
            "local_path",
            "size_bytes",
            "last_error",
        ])
        writer.writeheader()
        for item in ctx.obj.session.exec(MediaItem.iter_all()):
            writer.writerow(
                {
                    "id": item.id,
                    "kid_name": item.kid.name,
                    "kind": item.kind,
                    "captured_at": item.captured_at.isoformat(),
                    "caption": item.caption,
                    "local_path": item.local_path,
                    "size_bytes": item.size_bytes,
                    "last_error": item.last_error,
                }
            )
            count += 1
    console.print(f"Wrote {count} row(s) to [bold]{dest}[/]")


@app.command()
def version() -> None:
    """Print the version."""
    console.print(__version__)


def main() -> None:  # pragma: no cover
    try:
        app()
    except AuthError as exc:
        error_console.print(f"[red]Authentication error:[/] {exc}")
        sys.exit(1)
    except ProcareError as exc:
        error_console.print(f"[red]Error:[/] {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
