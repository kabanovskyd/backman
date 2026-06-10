#!/usr/bin/env python3

import sys
import subprocess
import yaml
import os
import pathlib
import concurrent.futures
import click
import tomllib
import google_crc32c
import struct
import shutil
import base64
import gspread
import pandas as pd

from crontab import CronTab
from datetime import datetime
from croniter import croniter
from importlib.metadata import version, PackageNotFoundError
from google.oauth2.service_account import Credentials
from rich.progress import (
    Progress, SpinnerColumn, BarColumn,
    TextColumn, TimeElapsedColumn, MofNCompleteColumn,
)
from rich.console import Console

console = Console()

from google.cloud import storage


BACKFILE_PATH = pathlib.Path('./backman/backfile')
HISTORY_PATH = pathlib.Path('./backman/history')


def manual(ctx, param, value):

    if not value or ctx.resilient_parsing:
        return

    print("""
      ------------------------------------------------------------------
      ██████╗  █████╗  ██████╗██╗  ██╗     ███╗   ███╗ █████╗ ███╗   ██╗    
      ██╔══██╗██╔══██╗██╔════╝██║ ██╔╝     ████╗ ████║██╔══██╗████╗  ██║   
      ██████╔╝███████║██║     █████╔╝█████╗██╔████╔██║███████║██╔██╗ ██║  
      ██╔══██╗██╔══██║██║     ██╔═██╗╚════╝██║╚██╔╝██║██╔══██║██║╚██╗██║  
      ██████╔╝██║  ██║╚██████╗██║  ██╗     ██║ ╚═╝ ██║██║  ██║██║ ╚████║  
      ╚═════╝ ╚═╝  ╚═╝ ╚═════╝╚═╝  ╚═╝     ╚═╝     ╚═╝╚═╝  ╚═╝╚═╝  ╚═══╝
      -------------------- Back-man wird backen 🥧 ---------------------

  backman — automated lab data backup tool                                                                                           
                                                                                                                                     
  USAGE                                                                                                                              
    backman <command> [options]                                                                                                      
                  
  COMMANDS

    Setup
    ─────────────────────────────────────────────────────────────────────
    init                         Initialize a new Backfile in the current directory                                                  
    set auth <auth_file>         Set the GCP credentials JSON file
    set bucket <dir>:<bucket> .. Assign a GCS bucket to a directory (* for all)                                                      
    sync <url> <creds>           Sync directory config from a Google Sheet                                                           
    unsync                       Remove Google Sheet sync; use Backfile only                                                         
                                                                                                                                     
    Tracking      
    ─────────────────────────────────────────────────────────────────────                                                            
    add <dir>:<subdir> ...       Add a directory/subdirectory pair to tracking
    add --file <file>            Add directories listed in a file (one per line)                                                     
    exclude <dir> ...            Pause tracking for specified directories                                                            
    include <dir> ...            Resume tracking for specified directories                                                           
    config                       Display current Backfile / Google Sheet config                                                      
                  
    Backup & Restore                                                                                                                 
    ─────────────────────────────────────────────────────────────────────
    status                       Show outdated/missing files across tracked dirs                                                     
    update                       Upload missing or changed files to GCS                                                              
      --all                        Re-upload all files regardless of change status
      --jobs <n>      (default 4)  Parallel upload workers                                                                                                           
    verify                       Compare local CRC32c checksums against GCS                                                          
    restore <dir> ...            Download backup from GCS to local disk                                                              
      <dir>:<subdir>               Restore a specific subdirectory                                                                   
      <dir>:*                      Restore all subdirs for a directory                                                               
      *                            Restore all tracked directories                                                                   
                                                                                                                                     
  NOTES                                                                                                                              
    - Requires a GCP service account JSON key; set with: backman set <auth_file>
    - Directory format for add/restore: /absolute/path/to/dir:subdirname                                                             
    - Backfile (backfile.yaml) must exist in the working directory for most commands
            
    """)
    sys.exit(0)


def get_version():
    """
    Retrieve project version
    """
    try:
        return version("backman")
    except PackageNotFoundError:
        # Fallback: Read directly from pyproject.toml if running uninstalled
        pyproject_path = pathlib.Path(__file__).parent.parent / "pyproject.toml"
        if pyproject_path.exists():
            with open(pyproject_path, "rb") as f:
                data = tomllib.load(f)
                return data.get("project", {}).get("version", "unknown")
        else:
            print("Pyproject.toml file not found in root! Exiting...")
            sys.exit(1)


def file_crc32c_b64(
    path: str | pathlib.Path,
    chunk_size: int = 4*1024*1024
):
    """
    Compute the CRC32c checksum of a file
    """

    val = 0
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            val = google_crc32c.extend(val, chunk)

    # encode as big-endian uint32 -> base64
    return base64.b64encode(struct.pack(">I", val)).decode()


def retrieve_google_sheet(sheet_url, cred_path):
    try:
        # authenticate
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
        creds = Credentials.from_service_account_file(cred_path, scopes=scopes)
        gc = gspread.authorize(creds)

        # open sheet
        sh = gc.open_by_url(sheet_url)

    except Exception as e:
        print(f'Could not access the sheet at {sheet_url}: {e}')
        sys.exit(1)

    ws = sh.get_worksheet(0)

    header = ws.row_values(1)
    required_cols = ['Tracked', 'Directory', 'Subdirectory', 'Bucket', 'Last Backup']
    missing = [c for c in required_cols if c not in header]
    if missing:
        print(f"ERROR: Google Sheet is missing required columns: {missing}")
        sys.exit(1)
    col_map = {name: header.index(name) + 1 for name in required_cols}

    status_df = pd.DataFrame(ws.get_all_records())
    if status_df.empty:
        return ws, pd.DataFrame(columns=required_cols), {}, col_map
    if status_df.duplicated(subset=['Directory', 'Subdirectory']).any():
        dup_rows = status_df[status_df.duplicated(subset=['Directory', 'Subdirectory'], keep=False)]
        dup_dir = dup_rows['Directory'].unique()
        dup_subdir = dup_rows['Subdirectory'].unique()
        print(f'ERROR: Google Sheet contains duplicate entries for {dup_dir}/{dup_subdir}')
        print('Please remove the duplicate rows and re-run the command.')
        sys.exit(1)
    status_df = status_df[status_df['Tracked'] == 'YES']
    dirs = status_df['Directory'].unique().tolist()

    target_directories = {}
    for dir in dirs:
        target_directories[dir] = {}
        upload_bucket = status_df[status_df['Directory'] == dir]['Bucket'].unique().tolist()
        if len(upload_bucket) > 1:
            print(f'ERROR: more than one bucket specified for a single directory: {dir} - {upload_bucket}')
            print('Please ensure that only one bucket is specified per directory and re-run.')
            sys.exit(1)
        target_directories[dir]['bucket'] = upload_bucket[0]
        target_directories[dir]['active'] = True
        target_directories[dir]['subdirs'] = status_df[status_df['Directory'] == dir]['Subdirectory'].tolist()

    return ws, pd.DataFrame(ws.get_all_records()), target_directories, col_map


def prompt_choice(prompt, valid_options):
    while True:
        response = input(prompt).strip().lower()
        if response in valid_options:
            return response
        print(f"Invalid input. Valid options are: {', '.join(valid_options)}")


def upload_parallel(bucket_name, items, directory, rel_directory, max_workers, bar_handler, task):
    failed = []

    def upload_one(item):
        local_path = item["path"]
        rel_path = os.path.relpath(local_path, directory)
        remote_uri = f"gs://{bucket_name}/{rel_directory}/{rel_path}"
        try:
            subprocess.run(
                ["gcloud", "storage", "cp", local_path, remote_uri],
                capture_output=True,
                text=True,
                check=True
            )
            bar_handler.advance(task, 1)
            return True
        except subprocess.CalledProcessError as e:
            bar_handler.advance(task, 1)
            return (local_path, e.stderr.strip())

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(upload_one, item) for item in items]
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            if result is not True:
                failed.append(result)

    if failed:
        console.print(f"[red]WARNING:[/red] {len(failed)} file(s) failed to upload:")
        for path, err in failed:
            console.print(f"  [red]✗[/red] {path}: {err}")

    return len(failed) == 0


def find_files_to_upload(
    local_files: list[dict],
    remote_manifest: dict,
    directory: str,
    upload_all: bool = False,
    strict: bool = False
) -> list[dict]:

    """Return only local files that are missing or changed in GCS."""
    to_upload = []

    for file in local_files:
        abs_path = file['path']
        rel_path = os.path.relpath(abs_path, directory)
        folder = os.path.basename(directory)
        remote_key = folder + '/' + rel_path

        if remote_key not in remote_manifest:
            to_upload.append({**file, "reason": "missing"})
        elif file["size"] != remote_manifest[remote_key]["size"]:
            to_upload.append({**file, "reason": "modified"})
        elif upload_all:
            to_upload.append({**file, "reason": "all_flag"})
        elif strict:
            if file["crc32c"] != remote_manifest[remote_key]["crc32c"]:
                to_upload.append({**file, "reason": "checksum mismatch"})

    return to_upload


def retrieve_gcp_files(
    client,
    bucket,
    directory,
    subdir,
    return_blobs=False
) -> dict:

    prefix = f"{directory}/" if subdir == '*' else f"{directory}/{subdir}/"
    blobs = client.list_blobs(bucket, prefix=prefix)
    manifest = {}

    if return_blobs:
        return blobs

    for blob in blobs:
        manifest[blob.name] = {
            "size": blob.size,
            "updated": blob.updated,
            "crc32c": blob.crc32c
        }

    return manifest


def collect_files(root: str, subdir: str, checksum: bool, exclude_exts: tuple = ()) -> list[dict]:
    results = []
    skipped = []
    def _walk(path):
        try:
            entries = list(os.scandir(path))
        except PermissionError:
            print(f"Warning: permission denied, skipping {path}")
            return
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=True):
                    _walk(entry.path)
                elif entry.is_file(follow_symlinks=True):
                    if exclude_exts and pathlib.Path(entry.name).suffix.lower() in exclude_exts:
                        print(f"Warning: skipping {entry.path} (filtering files with {pathlib.Path(entry.name).suffix.lower()} extension)")
                        skipped.append(entry.path)
                        continue
                    stat = entry.stat()
                    if checksum:
                        results.append({
                            "path": entry.path,
                            "size": stat.st_size,
                            "mtime": stat.st_mtime,
                            "crc32c": file_crc32c_b64(entry.path)
                        })
                    else:
                        results.append({
                            "path": entry.path,
                            "size": stat.st_size,
                            "mtime": stat.st_mtime
                        })
            except PermissionError:
                print(f"Warning: permission denied, skipping {entry.path}")
                skipped.append(entry.path)

    if subdir == '*':
        path = pathlib.Path(root)
    else:
        path = pathlib.Path(root) / subdir

    _walk(path)

    return results, skipped


def write_history_event(event):
    history = {}
    if pathlib.Path(HISTORY_PATH).is_file():
        with open(HISTORY_PATH, 'r') as f:
            history = yaml.safe_load(f) or {}
    date = datetime.now().strftime('%Y-%m-%d')
    if date not in history:
        history[date] = []
    history[date].append(event)
    pathlib.Path(HISTORY_PATH).parent.mkdir(exist_ok=True)
    with open(HISTORY_PATH, 'w') as f:
        yaml.dump(history, f, default_flow_style=False)


@click.group(add_help_option=False)                
@click.version_option(version=get_version(), prog_name="backman")                                                                                
@click.option("--help", is_flag=True, is_eager=True, expose_value=False,
              callback=manual) #, help="Show this message and exit.") 
@click.pass_context
def cli(ctx):
    """backman — automated lab data backup tool."""
    ctx.ensure_object(dict)
    
    # skip config loading if init is being called
    if ctx.invoked_subcommand == "init":
        return
    
    # read the backfile and load contents
    backfile_path = pathlib.Path(BACKFILE_PATH)
    if not backfile_path.is_file():
        print("Not a backable directory (.backman not found). Run `backman init` to make this directory backable")
        sys.exit(1)
    try:
        with open(BACKFILE_PATH, 'r') as file:
            config = yaml.safe_load(file)
    except Exception as e:
        print(f"Could not load the Backfile: {e}")
        sys.exit(1)

    ctx.obj["config"] = config

    # skip the authentication process if setting auth file
    if ctx.invoked_subcommand == "set":
        return

    # store authentication key file as an environment variable
    if 'authentication_file' not in config:
        print("`authentication_file` field missing from Backfile! Add it manually or run `backman set auth <path/to/auth/file>`")
        sys.exit(1)
    if not pathlib.Path(config['authentication_file']).is_file():
        print(f'Authentication file {config['authentication_file']} does not exist!')
        sys.exit(1)
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = config['authentication_file']

    # make sure Backfile contains directories specified for tracking
    if 'directories' not in config:
        print('No directories specified for tracking! Run `backman add [directories]` to track and update directories.')
        sys.exit(1)
    ctx.obj["directories"] = config['directories']

    # make sure Google Sheets fields are initialized
    if 'google_sheet' not in config:
        config['google_sheet'] = {'sheet_url': '', 'sheet_credentials': ''}
    
    if 'sheet_url' not in config['google_sheet']:
        config['google_sheet']['sheet_url'] = ''
    
    if 'sheet_credentials' not in config['google_sheet']:
        config['google_sheet']['sheet_credentials'] = ''

    if config['google_sheet']['sheet_credentials'] is None:
        config['google_sheet']['sheet_credentials'] = ''
    
    if config['google_sheet']['sheet_url'] is None:
        config['google_sheet']['sheet_url'] = ''

    with open(BACKFILE_PATH, "w") as f:
        yaml.dump(config, f, default_flow_style=False)

    ctx.obj["config"] = config


@cli.command()
@click.pass_context
@click.option(
    '--strict',
    is_flag=True,
    help='Use checksum comparison to ensure that backed up files are identical to local ones'
)
def status(ctx, strict):
    """
    Display the status of all tracked directories, listing subdirectories containing outdated/missing files
    """

    # try connecting to GCP
    try:
        client = storage.Client()
    except Exception as e:
        print(f"Could not establish a connection to GCP: {e}")
        sys.exit(1)

    # initialize function variables
    upload_dict = {}
    total_items = 0
    target_directories = ctx.obj["directories"]
    config = ctx.obj["config"]
    sheet_url = config['google_sheet']['sheet_url']
    sheet_creds = config['google_sheet']['sheet_credentials']
    skipped_items = []

    # if backman is synced with a Google Sheet, read directory information from it
    if sheet_url.strip() != '':
        _, _, target_directories, _ = retrieve_google_sheet(sheet_url, sheet_creds)

    # iterate over tracked directories and check for outdated/missing files
    for directory in target_directories:
        if not target_directories[directory]['active']:
            continue
        target_bucket = target_directories[directory]['bucket']
        target_subdirs = target_directories[directory]['subdirs']
        rel_directory = os.path.basename(directory)

        for subdir in target_subdirs:
            if subdir == 'ALL':
                subdir = '*'
            with console.status(f"[bold cyan][{subdir}][/bold cyan] Scanning...", spinner="dots"):
                items, skipped = collect_files(directory, subdir, strict)
                skipped_items.extend(skipped)
                gcp_items = retrieve_gcp_files(client, target_bucket, rel_directory, subdir, return_blobs=False)

            to_upload = find_files_to_upload(items, gcp_items, directory, strict=strict)

            # track which subdirectories contain missing/outdated files
            if len(to_upload) > 0:
                upload_dict[(directory, subdir)] = to_upload
                total_items += len(to_upload)

    # display results
    if len(skipped_items) > 0:
        print("WARNING - the following items were skipped:")
        for file in skipped_items:
            print(f" - {file}")

    if len(upload_dict) > 0:
        print("\n======= OUTDATED ITEMS =======\n")
        if total_items > 20:
            # print compressed subdirectory overview
            opt = prompt_choice(f"Print all {total_items} items? (y/[n]): ", ['yes', 'y', 'no', 'n', ''])
            if opt in ['no', 'n', '']:
                print("Displaying summary of tracked directories:")
                # iterate over directories with missing/outdated files and print how many files need to be updated
                for (dir, subdir) in upload_dict:
                    files = upload_dict[(dir, subdir)]
                    modified = len([file for file in files if file['reason'] == 'modified'])
                    missing = len([file for file in files if file['reason'] == 'missing'])
                    mismatch = len([file for file in files if file['reason'] == 'checksum mismatch']) if strict else 0

                    print(f'- {dir} — {subdir}: {len(files)} files out of date')
                    if modified > 0:
                        print(f'  • {modified} modified')
                    if missing > 0:
                        print(f'  • {missing} missing')
                    if mismatch > 0:
                        print(f'  • {mismatch} have checksum mismatch')
            else:
                # print every file + reason for backing up
                for (dir, subdir) in upload_dict:
                    print(f"{dir} — {subdir}:")
                    for file in upload_dict[(dir, subdir)]:
                        print(f"- {file['path']} ({file['reason']})")
        else:
            for (dir, subdir) in upload_dict:
                print(f"{dir} — {subdir}:")
                for file in upload_dict[(dir, subdir)]:
                    print(f"- {file['path']} ({file['reason']})")
        
        print()
        sys.exit(0)
    else:
        print('Everything up to date!\n')
        sys.exit(0)


@cli.command()
@click.pass_context
@click.option(
    "--upload_all",
    is_flag=True,
    default=False,
    help="Back up all files in tracked subdirectories"
)
@click.option(
    "--jobs",
    default=4,
    show_default=True,
    help="Parallel upload workers"
)
@click.option(
    '--strict',
    is_flag=True,
    help='Use checksum comparison to ensure that backed up files are identical to local ones'
)
@click.option(
    '--exclude-ext',
    default='',
    metavar='EXTS',
    help='Comma-separated list of extensions to skip (e.g. --exclude-ext .fastq,.bam,.gz).'
)
def update(ctx, jobs, upload_all, strict, exclude_ext):
    """Run the backup on tracked directories, backing up any outdated/missing files."""

    # try connecting to GCP
    try:
        client = storage.Client()
    except Exception as e:
        print(f"Could not establish a connection to GCP: {e}")
        sys.exit(1)

    # initialize function variables
    target_directories = ctx.obj["directories"]
    config = ctx.obj["config"]
    sheet_url = config['google_sheet']['sheet_url']
    sheet_creds = config['google_sheet']['sheet_credentials']
    all_skipped = []
    backup_history = {}

    # normalise exclude_ext: split on commas, lowercase, ensure leading dot
    exclude_exts = tuple(
        e.lower() if e.startswith('.') else f'.{e.lower()}'
        for e in (e.strip() for e in exclude_ext.split(','))
        if e.strip()
    )

    # if backman is synced with a Google Sheet, read directory information from it
    if sheet_url.strip() != '':
        ws, df, target_directories, col_map = retrieve_google_sheet(sheet_url, sheet_creds)

    # iterate over tracked directories and back up outdated/missing files
    for directory in target_directories.keys():
        if not target_directories[directory].get("active", True):
            continue
        target_subdirs = target_directories[directory]["subdirs"]
        target_bucket = target_directories[directory]["bucket"]
        rel_directory = os.path.basename(directory)

        # iterate over tracked subdirectories
        for subdir in target_subdirs:
            # `ALL` keyword allows globbing (automatic processing of all subdirectories in a dir)
            sheet_subdir = subdir
            if subdir == 'ALL':
                subdir = '*'

            with console.status(f"[bold cyan][{subdir}][/bold cyan] Scanning...", spinner="dots"):
                items, skipped = collect_files(directory, subdir, strict, exclude_exts)
                gcp_items = retrieve_gcp_files(client, target_bucket, rel_directory, subdir)

            to_upload = find_files_to_upload(items, gcp_items, directory, upload_all, strict)
            if len(skipped) > 0:
                all_skipped.extend(skipped)

            if not to_upload:
                console.print(f"[green]✓[/green] [bold]{subdir}[/bold] — nothing to upload.")
                continue

            console.print(f"[bold]{subdir}[/bold] — {len(to_upload)} file(s) to upload.")
            if sheet_url != '':
                # update the `Last Backup` column in the Google Sheet to reflect ongoing backup
                matching_rows = df.index[(df['Directory'] == directory) & (df['Subdirectory'] == sheet_subdir)].tolist()
                for ind in matching_rows:
                    ws.update_cell(ind + 2, col_map['Last Backup'], 'In progress')

            with Progress(
                SpinnerColumn(),
                TextColumn("[bold blue]{task.description}"),
                BarColumn(),
                MofNCompleteColumn(),
                TextColumn("•"),
                TimeElapsedColumn(),
                console=console,
            ) as progress:
                task = progress.add_task(f"Uploading [bold]{subdir}[/bold]...", total=len(to_upload))
                # upload the files directly
                upload_success = upload_parallel(
                    bucket_name=target_bucket,
                    items=to_upload,
                    directory=directory,
                    rel_directory=rel_directory,
                    max_workers=jobs,
                    bar_handler=progress,
                    task=task,
                )
            if upload_success:
                if directory not in backup_history:
                    backup_history[directory] = {}
                backup_history[directory][subdir] = len(to_upload)

            if upload_success and sheet_url != '':
                # update the `Last Backup` column in the Google Sheet with the latest backup date/time
                now = datetime.now()
                for ind in matching_rows:
                    ws.update_cell(ind + 2, col_map['Last Backup'], now.strftime("%Y-%m-%d %H:%M"))

    # list any files that were skipped
    if len(all_skipped) > 0:
        print('WARNING: the following files have NOT been uploaded due to insufficient permissions:')
        for file in all_skipped:
            print(f' - {file}')

    if backup_history:
        write_history_event({'type': 'backup', 'dirs': backup_history})


@cli.command()
@click.pass_context
@click.argument("dirs", nargs=-1, required=True)
def exclude(ctx, dirs):
    """
    Exclude specified directories from future backups, but keep them in the config file / Google Sheet.
    """

    # load the configuration file contents
    config = ctx.obj["config"]
    sheet_url = config['google_sheet']['sheet_url']
    sheet_creds = config['google_sheet']['sheet_credentials']
    excluded = []
    inactive = []

    # strip trailing slashes
    dirs = [dir[:-1] if dir.endswith('/') and not len(dir) == 1 else dir for dir in dirs]

    # update the Google Sheet fields if synced with a sheet
    if sheet_url.strip() != '':
        ws, df, _, col_map = retrieve_google_sheet(sheet_url, sheet_creds)

        # verify that all specified directories are present in the sheet
        if any(directory not in df['Directory'].values for directory in dirs) and dirs != ['all']:
            print('\nThe following directories are not present in the Google Sheet:\n')
            for directory in dirs:
                if directory not in df['Directory'].values:
                    print(f' - {directory}')
            print('\nPlease make sure all listed directories are present in the Google Sheet and re-run the command.\n')
            sys.exit(1)

        # iterate through the specified directories and change their tracking status to `NO`
        for dir in dirs:
            for ind, row in df.iterrows():
                row_dir = row['Directory'][:-1] if row['Directory'].endswith('/') else row['Directory']
                if row_dir == dir or dir == 'all':
                    if row['Tracked'] == 'YES':
                        ws.update_cell(ind + 2, col_map['Tracked'], 'NO')
                        excluded.append(row_dir)
                    else:
                        inactive.append(row_dir)

    else:
        # globbing collects all directories
        if dirs == ['all']:
            dirs = config['directories']

        # make sure all specified directories are present in the backfile
        if any(directory not in config['directories'] for directory in dirs):
            print('\nThe following directories are not present in the backfile:\n')
            for directory in dirs:
                if directory not in config['directories']:
                    print(f' - {directory}')
            print('\nPlease make sure all listed directories are present in the backfile and re-run the command.\n')
            sys.exit(1)

        # iterate over all specified directories and set their tracking status to `False`
        for directory in dirs:
            if config['directories'][directory]['active'] == True:
                config['directories'][directory]['active'] = False
                excluded.append(directory)
            else:
                inactive.append(directory)
        
        # write new config values to backfile
        with open(BACKFILE_PATH, "w") as f:
            yaml.dump(config, f, default_flow_style=False)
        
    # print status update info
    if len(excluded) > 0:
        print('\nThe following directories have been excluded from tracking:\n')
        for dir in excluded:
            print(f' - {dir}')
        write_history_event({'type': 'exclusion', 'dirs': {dir: [] for dir in excluded}})

    if len(inactive) > 0:
        print('\nThe following directories are already not being tracked:\n')
        for dir in inactive:
            print(f' - {dir}')
    print()


@cli.command()
@click.pass_context
@click.argument("dirs", nargs=-1, required=True)
def include(ctx, dirs):
    """
    Include specified directories from the config file / Google Sheet in future backups.
    """

    # load the configuration file contents
    config = ctx.obj["config"]
    sheet_url = config['google_sheet']['sheet_url']
    sheet_creds = config['google_sheet']['sheet_credentials']
    included = []
    active = []

    # strip trailing slashes
    dirs = [dir[:-1] if dir.endswith('/') and not len(dir) == 1 else dir for dir in dirs]

    # update the Google Sheet fields if synced with a sheet
    if sheet_url != '':
        ws, df, _, col_map = retrieve_google_sheet(sheet_url, sheet_creds)

        # verify that all specified directories are present in the sheet
        if any(directory not in df['Directory'].values for directory in dirs) and dirs != ['all']:
            print('\nThe following directories are not present in the Google Sheet:\n')
            for directory in dirs:
                if directory not in df['Directory'].values:
                    print(f' - {directory}')
            print('\nPlease make sure all listed directories are present in the Google Sheet and re-run the command.\n')
            sys.exit(1)

        # iterate through specified directories and change their tracking status to `YES`
        for dir in dirs:
            for ind, row in df.iterrows():
                row_dir = row['Directory'][:-1] if row['Directory'].endswith('/') else row['Directory']
                if row_dir == dir or dir == 'all':
                    if row['Tracked'] == 'NO':
                        ws.update_cell(ind + 2, col_map['Tracked'], 'YES')
                        included.append(row_dir)
                    else:
                        active.append(row_dir)
    else:
        # globbing collects all directories
        if dirs == ['all']:
            dirs = config['directories']

        # make sure all specified directories are present in the backfile
        if any(directory not in config['directories'] for directory in dirs):
            print('\nThe following directories are not present in the backfile:\n')
            for directory in dirs:
                if directory not in config['directories']:
                    print(f' - {directory}')
            print('\nPlease make sure all listed directories are present in the backfile and re-run the command.\n')
            sys.exit(1)

        # iterate over all specified directories and set their tracking status to `True`
        for directory in dirs:
            if config['directories'][directory]['active'] == False:
                config['directories'][directory]['active'] = True
                included.append(directory)
            else:
                active.append(directory)
        
        # write updated field values to backfile
        with open(BACKFILE_PATH, "w") as f:
            yaml.dump(config, f, default_flow_style=False)

    # print status update info
    if len(included) > 0:
        print('\nThe following directories have been included in tracking:\n')
        for dir in included:
            print(f' - {dir}')
        write_history_event({'type': 'inclusion', 'dirs': {dir: [] for dir in included}})

    if len(active) > 0:
        print('\nThe following directories are already being tracked:\n')
        for dir in active:
            print(f' - {dir}')
    print()


@cli.command()
def init():
    # warn the user that creating a new backfile will overwrite the existing one
    print()
    if pathlib.Path(BACKFILE_PATH).is_file():
        print('WARNING: you are about to overwrite the existing Backfile - this will delete ALL data about currently tracked directories!')
        opt = prompt_choice('Are you sure you want to continue? (y/[n]): ', ['yes', 'y', 'no', 'n', ''])
        if opt in ['no', 'n', '']:
            sys.exit(0)

    # obtain a path to a valid authentication key
    print('Creating Backfile...')
    auth_path = input("Please provide a path to a valid Google authentication key file: ")
    while not pathlib.Path(auth_path).is_file():
        auth_path = input(f"{auth_path} is not a file.\nPlease provide a path to a valid Google authentication key file: ")

    # set the fields in the config data structure and write it to the new backfile
    config = {}
    config['authentication_file'] = str(auth_path)
    config['google_sheet'] = {'sheet_url': '', 'sheet_credentials': ''}
    config['directories'] = {}
    pathlib.Path('./backman').mkdir(exist_ok=True)

    with open(BACKFILE_PATH, "w") as file:
        yaml.dump(config, file, default_flow_style=False)

    write_history_event({'type': 'creation'})
    print("Backfile created!\n")


@cli.group()
@click.pass_context
def set(ctx):
    """Set configuration values"""
    ctx.ensure_object(dict)

@set.command()
@click.pass_context
@click.argument('path', nargs=1, required=True)
def auth(ctx, path):
    config = ctx.obj['config']
    if not pathlib.Path(path).is_file():
        print(f'{path} not found.')
        sys.exit(1)

    config['authentication_file'] = path
    print(f'\nSet {path} as the authentication key file.\n')
    with open(BACKFILE_PATH, "w") as file:
        yaml.dump(config, file, default_flow_style=False)

@set.command()
@click.pass_context
@click.argument('names', nargs=-1, required=True)
def bucket(ctx, names):
    config = ctx.obj['config']
    sheet_url = config['google_sheet']['sheet_url']
    sheet_creds = config['google_sheet']['sheet_credentials']

    if sheet_url != '':
        ws, df, _, col_map = retrieve_google_sheet(sheet_url, sheet_creds)

        # verify that all specified directories are present in the sheet
        dirs = [name.split(':')[0] for name in names]
        dirs = [dir[:-1] if dir.endswith('/') else dir for dir in dirs]
        dirs_not_in_gs = [dir not in df['Directory'].values and dir != '*' for dir in dirs]
        if any(dirs_not_in_gs):
            print('\nThe following directories are not present in the Google Sheet:\n')
            for dir in dirs:
                if dir not in df['Directory'].values and dir != '*':
                    print(f' - {dir}')
            print('\nPlease make sure all listed directories are present in the Google Sheet and re-run the command.\n')
            sys.exit(1)

        for address in names:
            if ':' not in address or len(address.split(':')) != 2:
                print("Usage: backman set bucket <directory>:<bucket>")
                sys.exit(1)
            directory, bucket_addr = address.split(":")
            directory = directory[:-1] if directory.endswith('/') else directory

            for ind, row in df.iterrows():
                row_dir = row['Directory'][:-1] if row['Directory'].endswith('/') else row['Directory']
                if row_dir == directory or directory == '*':
                    ws.update_cell(ind + 2, col_map['Bucket'], bucket_addr)
    
            if directory == '*':
                print(f'Set the destination bucket for all directories to {bucket_addr}')
            else:
                print(f'Set the destination bucket for {directory} to {bucket_addr}')
    else:
        if len(config['directories']) == 0:
            print("Cannot set destination bucket as no directories are specified.")
            print("Please add directories for tracking by running `backman add <directory>:<subdirectory>`")
            sys.exit(1)
        for address in names:
            if ':' not in address or len(address.split(':')) != 2:
                print("Usage: backman set bucket <directory>:<bucket>")
                sys.exit(1)
            directory, bucket_addr = address.split(":")
            directory = directory[:-1] if directory.endswith('/') else directory
            if directory == "*":
                for dir in config['directories']:
                    config['directories'][dir]['bucket'] = bucket_addr
            else:
                if directory not in config['directories']:
                    print(f"Directory {directory} not found in Backfile! Please add it with `backman add {directory}:<subdirectory>`")
                    sys.exit(1)
                config['directories'][directory]['bucket'] = bucket_addr

            if directory == '*':
                print(f'Set the destination bucket for all directories to {bucket_addr}')
            else:
                print(f'Set the destination bucket for {directory} to {bucket_addr}')

        # dump the updated config dictionary contents into the backfile
        with open(BACKFILE_PATH, "w") as file:
            yaml.dump(config, file, default_flow_style=False)

    bucket_assignments = {}
    for address in names:
        directory, bucket_addr = address.split(":")
        directory = directory[:-1] if directory.endswith('/') else directory
        if directory == "*":
            for dir in config['directories']:
                bucket_assignments[dir] = bucket_addr
        else:
            bucket_assignments[directory] = bucket_addr
    write_history_event({'type': 'bucket', 'dirs': bucket_assignments})


@cli.command()
@click.pass_context
def config(ctx):
    config = ctx.obj['config']
    sheet_url = config['google_sheet']['sheet_url']
    sheet_creds = config['google_sheet']['sheet_credentials']

    if sheet_url.strip() != '':
        print('\n============= GOOGLE SHEET SUMMARY =============')
        _, df, _, _ = retrieve_google_sheet(sheet_url, sheet_creds)
        if 'YES' in df['Tracked'].tolist():
            print(f'\nTracked directories:')
            tracked_dirs = df[df['Tracked'] == 'YES']['Directory'].unique().tolist()
            for dir in tracked_dirs:
                tracked_subdirs = df.loc[(df['Directory'] == dir) & (df['Tracked'] == 'YES')]
                bucket = tracked_subdirs['Bucket'].tolist()[0]
                print(f'\n• {dir}')
                print(f'  bucket: {bucket}')
                print('  subdirs:')
                for subdir in tracked_subdirs['Subdirectory'].tolist():
                    print(f'   - {subdir}')

        if 'NO' in df['Tracked'].tolist():
            print(f'\nUntracked directories:')
            tracked_dirs = df[df['Tracked'] == 'NO']['Directory'].unique().tolist()
            for dir in tracked_dirs:
                tracked_subdirs = df.loc[(df['Directory'] == dir) & (df['Tracked'] == 'NO')]
                bucket = tracked_subdirs['Bucket'].tolist()[0]
                print(f'\n• {dir}')
                print(f'  bucket: {bucket}')
                print('  subdirs:')
                for subdir in tracked_subdirs['Subdirectory'].tolist():
                    print(f'   - {subdir}')
                
        print()
        sys.exit(0)   


    print('\n============= BACKFILE SUMMARY =============')
    print(f'Authentication file: {config['authentication_file']}')
    if any(config['directories'][directory]['active'] for directory in config['directories']):
        print(f'\nTracked directories:')
        for directory in config['directories']:
            path = directory
            directory = config['directories'][directory]
            if directory['active']:
                print(f'\n• {path}')
                if 'bucket' in directory:
                    print(f'  bucket: {directory['bucket']}')
                else:
                    print(f'  bucket: ')
                print(f'  subdirs:')
                for subdir in directory['subdirs']:
                    print(f'   - {subdir}')
    if not all(config['directories'][directory]['active'] for directory in config['directories']):
        print(f'\nUntracked directories:')
        for directory in config['directories']:
            path = directory
            directory = config['directories'][directory]
            if not directory['active']:
                print(f'\n• {path}')
                if 'bucket' in directory:
                    print(f'  bucket: {directory['bucket']}')
                else:
                    print(f'  bucket: ')
                print(f'  subdirs:')
                for subdir in directory['subdirs']:
                    print(f'   - {subdir}')

    print()
    sys.exit(0)


@cli.command()
@click.pass_context
@click.argument("dirs", nargs=-1, required=True)
def add(ctx, dirs):
    config = ctx.obj["config"]

    if dirs[0] == '--file':
        if len(dirs) < 2:
            print('Usage: backman add --file [file_with_directories]')
            sys.exit(1)
        dir_file = dirs[1]
        if len(dirs) > 2:
            print('Usage: backman add --file [file_with_directories]')
            sys.exit(1)
        if not pathlib.Path(dir_file).is_file():
            print(f'File {dir_file} does not exist!')
            sys.exit(1)
        
        dirs = []
        with open(dir_file, 'r') as file:
            for line in file:
                dirs.append(line.strip())

    added_dirs = {}
    for dir in dirs:
        if ':' in dir:
            if len(dir.split(':')) != 2:
                print('Please provide subdirectories as a list of [directory]:[subdirectory] pairs')
                sys.exit(1)

            directory, subdirectory = dir.split(':')
            directory = directory[:-1] if directory.endswith('/') else directory
            if not pathlib.Path(directory).is_dir():
                print(f'{directory} is not a directory!')
                sys.exit(1)

            if subdirectory == '*':
                subdirs = [d.name for d in pathlib.Path(directory).glob('*') if d.is_dir()]
                if directory not in added_dirs:
                    added_dirs[directory] = []
                if directory not in config['directories']:
                    config['directories'][directory] = {'subdirs': [], 'active': True, 'bucket': ''}
                for subdir in subdirs:
                    if 'subdirs' not in config['directories'][directory]:
                        config['directories'][directory]['subdirs'] = [subdir]
                        added_dirs[directory].append(subdir)
                    if subdir not in config['directories'][directory]['subdirs']:
                        config['directories'][directory]['subdirs'].append(subdir)
                        added_dirs[directory].append(subdir)
            else:
                if not (pathlib.Path(directory) / subdirectory).is_dir():
                    print(f'{subdirectory} is not a directory!')
                    sys.exit(1)

                if directory in config['directories']:
                    if not 'subdirs' in config['directories'][directory]:
                        config['directories'][directory]['subdirs'] = []
                    if len(config['directories'][directory]) == 0:
                        config['directories'][directory]['subdirs'] = subdirectory
                    else:
                        if subdirectory not in config['directories'][directory]['subdirs']:
                            config['directories'][directory]['subdirs'].append(subdirectory)
                            if directory in added_dirs:
                                added_dirs[directory].append(subdirectory)
                            else:
                                added_dirs[directory] = [subdirectory]
                else:
                    config['directories'][directory] = {'subdirs': [subdirectory], 'active': True, 'bucket': ''}
                    added_dirs[directory] = [subdirectory]

        else:
            directory = dir[:-1] if dir.endswith('/') else dir
            if not pathlib.Path(dir).is_dir():
                print(f'{directory} is not a directory!')
                sys.exit(1)
            if directory in config['directories']:
                print(f'{directory} is already being tracked!')
                sys.exit(1)
            config['directories'][directory] = {'subdirs': [], 'active': True, 'bucket': ''}
            if not directory in added_dirs:
                added_dirs[directory] = []

    for directory in added_dirs:
        config['directories'][directory]['active'] = True
        if 'subdirs' not in config['directories'][directory]:
            config['directories'][directory]['subdirs'] = []
    
    with open(BACKFILE_PATH, "w") as f:
        yaml.dump(config, f, default_flow_style=False)
    
    if any([len(added_dirs[dir]) > 0 for dir in added_dirs]):
        print('\nThe following directories have been added to tracking:\n')
        for dir in added_dirs:
            if len(added_dirs[dir]) > 0:
                print(f'{dir}')
                for subdir in added_dirs[dir]:
                    print(f'  - {subdir}')
        print()
        write_history_event({'type': 'addition', 'dirs': {dir: added_dirs[dir] for dir in added_dirs if len(added_dirs[dir]) > 0}})
    else:
        print('Nothing to add! (All directories/subdirectories already present)')


@cli.command()
@click.pass_context
@click.argument("dirs", nargs=-1, required=True)
def remove(ctx, dirs):
    config = ctx.obj["config"]

    if dirs[0] == '--file':
        if len(dirs) < 2:
            print('Usage: backman remove --file [file_with_directories]')
            sys.exit(1)
        dir_file = dirs[1]
        if len(dirs) > 2:
            print('Usage: backman remove --file [file_with_directories]')
            sys.exit(1)
        if not pathlib.Path(dir_file).is_file():
            print(f'File {dir_file} does not exist!')
            sys.exit(1)
        
        dirs = []
        with open(dir_file, 'r') as file:
            for line in file:
                dirs.append(line.strip())

    removed_dirs = {}
    for dir in dirs:
        if ':' in dir:
            if len(dir.split(':')) != 2:
                print('Please provide subdirectories as a list of [directory]:[subdirectory] pairs')
                sys.exit(1)

            directory, subdirectory = dir.split(':')
            directory = directory[:-1] if directory.endswith('/') else directory
            if not pathlib.Path(directory).is_dir():
                print(f'{directory} is not a directory!')
                sys.exit(1)
            if directory not in config['directories']:
                print(f"{directory} is not present in the tracking system!")
                sys.exit(1)
            if directory not in removed_dirs:
                removed_dirs[directory] = []

            if subdirectory == '*':
                removed_dirs[directory] = list(config['directories'][directory]['subdirs'])
            else:
                if subdirectory not in config['directories'][directory]['subdirs']:
                    print(f"{subdirectory} is not present as a subdirectory of {directory} in the tracking system!")
                    sys.exit(1)
                else:
                    removed_dirs[directory].append(subdirectory)

        else:
            dir = dir[:-1] if dir.endswith('/') else dir
            if not pathlib.Path(dir).is_dir():
                print(f'{dir} is not a directory!')
                sys.exit(1)
            if dir in config['directories']:
                removed_dirs[dir] = list(config['directories'][dir]['subdirs'])
            else:
                print(f'{dir} is not present in the tracking system!')
                sys.exit(1)

    for directory in removed_dirs:
        if len(removed_dirs[directory]) > 0:
            for subdir in list(removed_dirs[directory]):
                config['directories'][directory]['subdirs'].remove(subdir)
        
        if not config['directories'][directory]['subdirs']:
            del config['directories'][directory]
    
    with open(BACKFILE_PATH, "w") as f:
        yaml.dump(config, f, default_flow_style=False)
    
    print('\nThe following directories have been removed from tracking:\n')
    for dir in removed_dirs:
        if len(removed_dirs[dir]) > 0:
            print(f'{dir}')
            for subdir in removed_dirs[dir]:
                print(f'  - {subdir}')
    print()
    write_history_event({'type': 'removal', 'dirs': removed_dirs})


@cli.command()
@click.pass_context
@click.argument("url", nargs=1, required=True)
@click.argument("creds", nargs=1, required=True)
def sync(ctx, url, creds):
    config = ctx.obj["config"]
    print('>> WARNING: you are attempting to sync with a Google Sheet <<')
    print('backman will ONLY track the directories specified in the sheet')
    print('Directories specified in the Backfile will NOT be tracked!')
    resp = prompt_choice('Proceed? (y/[n]): ', ['n', 'no', 'y', 'yes', ''])
    if resp in ['n', 'no', '']:
        sys.exit(0)

    if not creds or creds == '':
        print(f'ERROR: invalid path to Google Sheet credentials file: {creds}')
        print('Please create a valid credentials file on the Google Cloud Console and re-run.')
        sys.exit(1)

    creds = pathlib.Path(creds)
    if not creds.is_file():
        print(f'ERROR: invalid path to Google Sheet credentials file: {creds}')
        print('Please create a valid credentials file on the Google Cloud Console and re-run.')
        sys.exit(1)

    if not url or url == '' or not 'docs.google.com/spreadsheets' in url:
        print(f'ERROR: invalid spreadsheet URL: {url}')
        sys.exit(1)

    try:
        # authenticate
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
        credentials = Credentials.from_service_account_file(creds, scopes=scopes)
        gc = gspread.authorize(credentials)

        # open sheet
        sh = gc.open_by_url(url)

    except Exception as e:
        print(f'Could not access the sheet at {url}: {e}')
        sys.exit(1)

    print(f'Successfully synced with the sheet at {url}!')
    config['google_sheet']['sheet_url'] = url
    config['google_sheet']['sheet_credentials'] = str(creds)

    with open(BACKFILE_PATH, "w") as f:
        yaml.safe_dump(config, f, default_flow_style=False)

    write_history_event({'type': 'sync', 'url': url})


@cli.command()
@click.pass_context
def unsync(ctx):
    # update the config object and remove the google sheet URL
    config = ctx.obj["config"]
    url = config['google_sheet']['sheet_url']
    config['google_sheet']['sheet_url'] = ''

    # check that the backfile is actually synced to a Google Sheet
    if url.strip() == '':
        print("Google Sheet is not synced - nothing to unsync from.\n")
        sys.exit(1)

    # print update info
    print(f'Successfully unsynced from the sheet at {url}.')
    print('NOTE: backman will now ONLY track the directories specified in the Backfile!\n')

    # write the updated config object to backfile
    with open(BACKFILE_PATH, "w") as f:
        yaml.dump(config, f, default_flow_style=False)

    write_history_event({'type': 'unsync', 'url': url})


@cli.command()
@click.pass_context
@click.argument("dirs", nargs=-1, required=True)
def restore(ctx, dirs):
    def _fmt_bytes(n):
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024:
                return f"{n:g} {unit}"
            n /= 1024
        return f"{n:g} TB"
    
    def _download_blobs(blobs, dest_dir, gcs_prefix, progress, task):
        dest = pathlib.Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)

        for blob in blobs:
            rel = blob.name[len(gcs_prefix):]
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            blob.download_to_filename(out)
            progress.advance(task)


    target_directories = ctx.obj["directories"]
    config = ctx.obj["config"]

    try:
        client = storage.Client()
    except Exception as e:
        print(f"Could not establish a connection to GCP: {e}")
        sys.exit(1)
    sheet_url = config['google_sheet']['sheet_url']
    sheet_creds = config['google_sheet']['sheet_credentials']
    dirs_to_restore = {}
    total_size = 0

    # if backman is synced with a Google Sheet, read directory information from it
    if sheet_url != '':
        _, _, target_directories, _ = retrieve_google_sheet(sheet_url, sheet_creds)

    if len(dirs) == 0:
        print('Usage: backman restore --dirs [directory1, directory1:subdirectory]')

    for dir in dirs:
        if dir == '*':
            for directory in target_directories.keys():
                dirs_to_restore[directory] = target_directories[directory]['subdirs']
            continue

        if ':' in dir:
            if len(dir.split(':')) != 2:
                print('Please provide subdirectories as a list of [directory]:[subdirectory] pairs')
                sys.exit(1)
            directory, subdirectory = dir.split(':')
            directory = directory[:-1] if directory.endswith('/') else directory
            if not pathlib.Path(directory).is_dir():
                print(f'{directory} is not a directory!')
                sys.exit(1)
            if subdirectory == '*':
                if directory not in target_directories:
                    print(f'ERROR: {directory} is not a tracked directory')
                    sys.exit(1)
                dirs_to_restore[directory] = target_directories[directory]['subdirs']
                continue
            if directory not in target_directories:
                print(f'ERROR: {directory} is not a tracked directory')
                sys.exit(1)
            if directory in dirs_to_restore:
                dirs_to_restore[directory].append(subdirectory)
            else:
                dirs_to_restore[directory] = [subdirectory]

        else:
            dir = dir[:-1] if dir.endswith('/') else dir
            if dir in target_directories.keys():
                dirs_to_restore[dir] = target_directories[dir]['subdirs']
            else:
                print(f'ERROR: {dir} is not a tracked directory')
                sys.exit(1)

    with console.status("[bold cyan]Calculating restore size...", spinner="dots"):
        for dir in dirs_to_restore:
            target_bucket = target_directories[dir]['bucket']
            rel_directory = os.path.basename(dir)
            for subdir in dirs_to_restore[dir]:
                effective_subdir = '*' if subdir == 'ALL' else subdir
                gcp_items = retrieve_gcp_files(client, target_bucket, rel_directory, effective_subdir, return_blobs=False)
                for item in gcp_items:
                    total_size += gcp_items[item]['size']

    total_memory = _fmt_bytes(total_size)
    _, _, free_disk = shutil.disk_usage(".")
    if total_size > free_disk:
        console.print(f'[red]ERROR:[/red] {_fmt_bytes(free_disk)} available on disk, but restoration requires {total_memory}')
        sys.exit(1)

    resp = prompt_choice(f'Restoring the backups will take up {total_memory} on disk. Proceed? (y/[n]): ', ['y', 'yes', 'n', 'no', ''])
    if resp in ['no', 'n', '']:
        sys.exit(0)

    restore_history = {}
    for dir in dirs_to_restore:
        current_dirs = [os.path.basename(p) for p in pathlib.Path.cwd().iterdir() if p.is_dir()]
        rel_directory = os.path.basename(dir)
        target_bucket = target_directories[dir]['bucket']
        if rel_directory in current_dirs:
            dirname = rel_directory + '_backup'
            counter = 0
            while dirname in current_dirs:
                counter += 1
                dirname = rel_directory + '_backup' + str(counter)
        else:
            dirname = rel_directory

        pathlib.Path(dirname).mkdir(exist_ok=True)

        for subdir in dirs_to_restore[dir]:
            effective_subdir = '*' if subdir == 'ALL' else subdir
            if effective_subdir == '*':
                subdir_dest = pathlib.Path(dirname)
                gcs_prefix = f"{rel_directory}/"
            else:
                subdir_dest = pathlib.Path(dirname) / subdir
                gcs_prefix = f"{rel_directory}/{subdir}/"
            subdir_dest.mkdir(parents=True, exist_ok=True)

            blobs = list(retrieve_gcp_files(client, target_bucket, rel_directory, effective_subdir, return_blobs=True))
            with Progress(
                SpinnerColumn(),
                TextColumn("[bold blue]{task.description}"),
                BarColumn(),
                MofNCompleteColumn(),
                TextColumn("•"),
                TimeElapsedColumn(),
                console=console,
            ) as progress:
                task = progress.add_task(f"Restoring [bold]{subdir}[/bold]...", total=len(blobs))
                _download_blobs(blobs, subdir_dest, gcs_prefix, progress, task)

            if dir not in restore_history:
                restore_history[dir] = {}
            restore_history[dir][subdir] = len(blobs)

    console.print("[green]✓[/green] All backups restored.")
    write_history_event({'type': 'restore', 'dirs': restore_history})


@cli.command()
@click.pass_context
def history(ctx):
    if not pathlib.Path(HISTORY_PATH).is_file():
        print('No history recorded yet.')
        return

    with open(HISTORY_PATH, 'r') as file:
        hist = yaml.safe_load(file) or {}

    if not hist:
        print('No history recorded yet.')
        return

    print('--------- DATA BACKUP LOG ---------\n')

    for date in hist:
        print(f'[{date}]:')
        for event in hist[date]:
            match event['type']:
                case 'creation':
                    print(' • [backfile initialized]')
                case 'bucket':
                    print(' • GCS bucket set for the following directories:')
                    for dir in event['dirs']:
                        print(f'   {dir} -> {event['dirs'][dir]}')
                case 'addition':
                    print(' • new directories added:')
                    for dir in event['dirs']:
                        print(f'   {dir}:')
                        if len(event['dirs'][dir]) > 0:
                            for subdir in event['dirs'][dir]:
                                print(f'   - {subdir}')
                case 'removal':
                    print(' • directories removed:')
                    for dir in event['dirs']:
                        print(f'   {dir}:')
                        if len(event['dirs'][dir]) > 0:
                            for subdir in event['dirs'][dir]:
                                print(f'   - {subdir}')
                case 'inclusion':
                    print(' • directories included in tracking:')
                    for dir in event['dirs']:
                        print(f'   {dir}:')
                        if len(event['dirs'][dir]) > 0:
                            for subdir in event['dirs'][dir]:
                                print(f'   - {subdir}')
                case 'exclusion':
                    print(' • directories excluded from tracking:')
                    for dir in event['dirs']:
                        print(f'   {dir}:')
                        if len(event['dirs'][dir]) > 0:
                            for subdir in event['dirs'][dir]:
                                print(f'   - {subdir}')
                case 'backup':
                    print(' • files backed up:')
                    for dir in event['dirs']:
                        print(f'   {dir}:')
                        for subdir in event['dirs'][dir]:
                            print(f'   - {subdir}: {event['dirs'][dir][subdir]} files')
                case 'restore':
                    print(' • files restored:')
                    for dir in event['dirs']:
                        print(f'   {dir}:')
                        for subdir in event['dirs'][dir]:
                            print(f'   - {subdir}: {event['dirs'][dir][subdir]} files')
                case 'sync':
                    print(f' • synced with a Google Sheet')
                    print(f'    - URL: {event['url']}')
                case 'unsync':
                    print(f' • unsynced from a Google Sheet')
                    print(f'    - URL: {event['url']}')
                case _:
                    print(f'Formatting error in ./backman/history: {event['type']}')
                    sys.exit(1)


@cli.command()
@click.argument("cron", nargs=-1, required=True)
def schedule(cron):
    """
    Parses and validates a cron string.
    
    Example: cron-tool "*/15 * * * *"
    Or:      cron-tool * * * * *
    """

    # Join parts into a single string (handles space-separated input automatically)
    cron_string = " ".join(cron)

    if not croniter.is_valid(cron_string):
        click.secho(f"Error: '{cron_string}' is not a valid cron expression.", fg="red", err=True)
        raise click.Abort()

    entry_point = shutil.which("backman") or f"{sys.executable} {pathlib.Path(__file__).resolve()}"
    log_path = pathlib.Path.home() / ".backman.log"
    command = f"{entry_point} update >> {log_path} 2>&1"

    tab = CronTab(user=True)
    job_comment = "regular_backman_job"
    tab.remove_all(comment=job_comment)

    job = tab.new(command=command, comment=job_comment)
    job.setall(cron_string)
    tab.write()
    print("Cron job scheduled successfully.")


@cli.command()
def unschedule():
    cron = CronTab(user=True)
    # Create a new job (avoid duplicates by checking for a unique comment)
    job_comment = "regular_backman_job"
    jobs = list(cron.find_comment(job_comment))
    if jobs:
        cron.remove_all(comment=job_comment) # Clean up old versions
        job = jobs[0]
        schedule = job.slices.render()
        print(f"Removed scheduled cron job ({schedule})")
    else:
        print("No cron jobs scheduled!")
    
    # Write to the system crontab
    cron.write()


@cli.command()
def jobs():
    cron = CronTab(user=True)
    # Create a new job (avoid duplicates by checking for a unique comment)
    job_comment = "regular_backman_job"
    jobs = list(cron.find_comment(job_comment))
    if jobs:
        job = jobs[0]
        schedule = job.slices.render()
        now = datetime.now()
        cron_iter = croniter(schedule, now)
        next_run = cron_iter.get_next(datetime)
        print(f"Next scheduled run: {next_run.strftime('%Y-%m-%d %H:%M:%S')}")
    else:
        print("No cron jobs scheduled!")


if __name__ == "__main__":
    cli()


    
