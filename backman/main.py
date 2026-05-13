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


EXCLUDE_EXTENSIONS = {
    #".pyc",
    #".pyo",
    #".json",
    #".env",
    #".bam",
    #".swp",   # vim swap files
}

EXCLUDE_DIRS = {
    #".git",
    #"__pycache__",
    #"node_modules",
    #".venv",
    #"venv",
    #"env",
    #"tmp",
    #"temp",
    #"scratch",
}


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
    status_df = pd.DataFrame(ws.get_all_records())
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

    return ws, pd.DataFrame(ws.get_all_records()), target_directories


def prompt_choice(prompt, valid_options):
    while True:
        response = input(prompt).strip().lower()
        if response in valid_options:
            return response
        print(f"Invalid input. Valid options are: {', '.join(valid_options)}")


def upload_parallel(bucket_name, items, directory, rel_directory, max_workers, bar_handler, task):
    def upload_one(item, bar_handler, task):
        local_path = item["path"]
        rel_path = os.path.relpath(local_path, directory)
        remote_uri = f"gs://{bucket_name}/{rel_directory}/{rel_path}"
        try:
            result = subprocess.run(
                ["gcloud", "storage", "cp", local_path, remote_uri],
                capture_output=True,
                text=True,
                check=True
            )
        except subprocess.CalledProcessError as e:
            print(f"Upload failed: {e.stderr}")

        bar_handler.advance(task, 1)
        # print(f"  Uploaded {remote_uri}", flush=True)

    # print(f"  Uploading {len(items)} file(s) via gcloud storage cp...", flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(upload_one, item, bar_handler, task) for item in items]
        for future in concurrent.futures.as_completed(futures):
            future.result()

    return True


def find_files_to_upload(
    local_files: list[dict],
    remote_manifest: dict,
    directory: str,
    upload_all: bool = False
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
        #elif file["crc32c"] != remote_manifest[remote_key]["crc32c"]:
        #    to_upload.append({**file, "reason": "checksum mismatch"})

    return to_upload


def retrieve_gcp_files(
    client,
    bucket,
    directory,
    subdir,
    return_blobs=False
) -> dict:

    blobs = client.list_blobs(bucket, prefix=f"{directory}/{subdir}/")
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


def collect_files(root: str, subdir: str,) -> list[dict]:
    results = []
    skipped = []
    def _walk(path):
        try:
            entries = list(os.scandir(path))
        except PermissionError:
            print(f"WARNING: permission denied, skipping {path}")
            return
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=True):
                    if entry.name not in EXCLUDE_DIRS:
                        _walk(entry.path)
                elif entry.is_file(follow_symlinks=True):
                    ext = os.path.splitext(entry.name)[1].lower()
                    if ext not in EXCLUDE_EXTENSIONS:
                        stat = entry.stat()
                        results.append({
                            "path": entry.path,
                            "size": stat.st_size,
                            "mtime": stat.st_mtime
                            #"crc32c": crc32c(entry.path)
                        })
            except PermissionError:
                print(f"Warning: permission denied, skipping {entry.path}")
                skipped.append(path / entry)

    if subdir == '*':
        path = pathlib.Path(root)
    else:
        path = pathlib.Path(root) / subdir

    _walk(path)

    return results, skipped


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
    backfile_path = pathlib.Path('backfile.yaml')
    if not backfile_path.is_file():
        print("backfile.yaml not found in project root!")
        sys.exit(1)
    try:
        with open('backfile.yaml', 'r') as file:
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

    with open("backfile.yaml", "w") as f:
        yaml.dump(config, f, default_flow_style=False)

    ctx.obj["config"] = config

    # try connecting to GCP
    try:
        ctx.obj["client"] = storage.Client()
    except Exception as e:
        print(f"Could not establish a connection to GCP: {e}")
        sys.exit(1)


@cli.command()
@click.pass_context
def status(ctx):
    """Display the status of all tracked directories, listing subdirectories containing outdated/missing files"""

    # initialize function variables
    upload_dict = {}
    total_items = 0
    target_directories = ctx.obj["directories"]
    config = ctx.obj["config"]
    client = ctx.obj["client"]
    sheet_url = config['google_sheet']['sheet_url']
    sheet_creds = config['google_sheet']['sheet_credentials']
    skipped_items = []

    # if backman is synced with a Google Sheet, read directory information from it
    if sheet_url.strip() != '':
        _, _, target_directories = retrieve_google_sheet(sheet_url, sheet_creds)
    
    # iterate over tracked directories and check for outdated/missing files
    for directory in target_directories:
        if not target_directories[directory]['active']:
            continue
        target_bucket = target_directories[directory]['bucket']
        target_subdirs = target_directories[directory]['subdirs']
        rel_directory = os.path.basename(directory)

        for subdir in target_subdirs:
            print(subdir)
            if subdir == 'ALL':
                subdir = '*'
                #target_subdirs = [item for item in pathlib.Path(directory).glob('*') if item.is_dir()]
                #continue
            with console.status(f"[bold cyan][{subdir}][/bold cyan] Scanning...", spinner="dots"):
                items, skipped = collect_files(directory, subdir)
                skipped_items.extend(skipped)
                gcp_items = retrieve_gcp_files(client, target_bucket, rel_directory, subdir, return_blobs=False)

            to_upload = find_files_to_upload(items, gcp_items, directory)

            # track which subdirectories contain missing/outdated files
            if len(to_upload) > 0:
                upload_dict[subdir] = to_upload
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
                for dir in upload_dict:
                    modified = len([file for file in upload_dict[dir] if file['reason'] == 'modified'])
                    missing = len([file for file in upload_dict[dir] if file['reason'] == 'missing'])
                    print(f'- {dir}: {len(upload_dict[dir])} files out of date')
                    if modified > 0:
                        print(f'  • {modified} modified')
                    if missing > 0:
                        print(f'  • {missing} missing')
            else:
                # print every file + reason for backing up
                for dir in upload_dict:
                    print(f"{dir}:")
                    for file in upload_dict[dir]:
                        print(f"- {file['path']} ({file['reason']})")
        else:
            for dir in upload_dict:
                print(f"{dir}:")
                for file in upload_dict[dir]:
                    print(f"- {file['path']} ({file['reason']})")
        
        print()
        sys.exit(0)
    else:
        print('Everything up to date!\n')
        sys.exit(0)


@cli.command()
@click.pass_context
@click.option("--upload_all", is_flag=True, default=False, help="Back up all files in tracked subdirectories")
@click.option("--jobs", default=4, show_default=True, help="Parallel upload workers")
def update(ctx, jobs, upload_all):
    """Run the backup on tracked directories, backing up any outdated/missing files."""

    # initialize function variables
    target_directories = ctx.obj["directories"]
    config = ctx.obj["config"]
    client = ctx.obj["client"]
    sheet_url = config['google_sheet']['sheet_url']
    sheet_creds = config['google_sheet']['sheet_credentials']
    credentials_path = pathlib.Path(config["authentication_file"])
    all_skipped = []

    # if backman is synced with a Google Sheet, read directory information from it
    if sheet_url.strip() != '':
        _, df, target_directories = retrieve_google_sheet(sheet_url, sheet_creds)

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
            if subdir == 'ALL':
                subdir = '*'

            with console.status(f"[bold cyan][{subdir}][/bold cyan] Scanning...", spinner="dots"):
                items, skipped = collect_files(directory, subdir)
                gcp_items = retrieve_gcp_files(client, target_bucket, rel_directory, subdir)

            to_upload = find_files_to_upload(items, gcp_items, directory, upload_all)
            if len(skipped) > 0:
                all_skipped.extend(skipped)

            if not to_upload:
                console.print(f"[green]✓[/green] [bold]{subdir}[/bold] — nothing to upload.")
                continue

            console.print(f"[bold]{subdir}[/bold] — {len(to_upload)} file(s) to upload.")
            if sheet_url != '':
                # update the `Last Backup` column in the Google Sheet to reflect ongoing backup
                df.loc[(df['Directory'] == directory) & (df['Subdirectory'] == subdir), 'Last Backup'] = 'In progress'

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
            if upload_success and sheet_url != '':
                # update the `Last Backup` column in the Google Sheet with the latest backup date/time
                now = datetime.now()
                df.loc[(df['Directory'] == directory) & (df['Subdirectory'] == subdir), 'Last Backup'] = now.strftime("%Y-%m-%d %H:%M")

    # list any files that were skipped
    if len(all_skipped) > 0:
        print('WARNING: the following files have NOT been uploaded due to insufficient permissions:')
        for file in all_skipped:
            print(f' - {file}')


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
        ws, df, _ = retrieve_google_sheet(sheet_url, sheet_creds)

        # verify that all specified directories are present in the sheet
        if any(directory not in df['Directory'].values for directory in dirs) and dirs != ['all']:
            print('\nThe following directories are not present in the Google Sheet:\n')
            for directory in dirs:
                if directory not in config['directories']:
                    print(f'- {directory}')
            print('\nPlease make sure all listed directories are present in the Google Sheet and re-run the command.\n')
            sys.exit(1)

        # iterate through the specified directories and change their tracking status to `NO`
        for dir in dirs:
            for ind, row in df.iterrows():
                row_dir = row['Directory'][:-1] if row['Directory'].endswith('/') else row['Directory']
                if row_dir == dir or dir == '*':
                    if row['Tracked'] == 'YES':
                        ws.update_cell(ind + 2, 1, 'NO')
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
                    print(f'- {directory}')
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
        with open("backfile.yaml", "w") as f:
            yaml.dump(config, f, default_flow_style=False)
        
    # print status update info
    if len(excluded) > 0:
        print('\nThe following directories have been excluded from tracking:\n')
        for dir in excluded:
            print(f'- {dir}')

    if len(inactive) > 0:
        print('\nThe following directories are already not being tracked:\n')
        for dir in inactive:
            print(f'- {dir}')
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
        ws, df, _ = retrieve_google_sheet(sheet_url, sheet_creds)

        # verify that all specified directories are present in the sheet
        if any(directory not in df['Directory'].values for directory in dirs) and dirs != ['all']:
            print('\nThe following directories are not present in the Google Sheet:\n')
            for directory in dirs:
                if directory not in config['directories']:
                    print(f'- {directory}')
            print('\nPlease make sure all listed directories are present in the Google Sheet and re-run the command.\n')
            sys.exit(1)
    
        # iterate through specified directories and change their tracking status to `YES`
        for dir in dirs:
            for ind, row in df.iterrows():
                row_dir = row['Directory'][:-1] if row['Directory'].endswith('/') else row['Directory']
                if row_dir == dir or dir == 'all':
                    if row['Tracked'] == 'NO':
                        ws.update_cell(ind + 2, 1, 'YES')
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
                    print(f'- {directory}')
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
        with open("backfile.yaml", "w") as f:
            yaml.dump(config, f, default_flow_style=False)

    # print status update info
    if len(included) > 0:
        print('\nThe following directories have been included in tracking:\n')
        for dir in included:
            print(f'- {dir}')

    if len(active) > 0:
        print('\nThe following directories are already being tracked:\n')
        for dir in active:
            print(f'- {dir}')
    print()


@cli.command()
def init():
    # warn the user that creating a new backfile will overwrite the existing one
    print()
    if pathlib.Path('./.backfile.yaml').is_file():
        print('WARNING: you are about to overwrite the existing Backfile - this will delete ALL data about currently tracked directories!')
        opt = prompt_choice('Are you sure you want to continue? (y/[n]): ', ['yes', 'y', 'no', 'n', ''])
        if opt in ['no', 'n', '']:
            sys.exit(0)

    # obtain a path to a valid authentication key
    print('Creating Backfile...')
    auth_path = input("Please provide a path to a valid Google authentication key file: ")
    while not pathlib.Path(auth_path).is_file:
        auth_path = input(f"{auth_path} is not a file.\nPlease provide a path to a valid Google authentication key file: ")

    # set the fields in the config data structure and write it to the new backfile
    config = {}
    config['authentication_file'] = str(auth_path)
    config['google_sheet'] = {'sheet_url': '', 'sheet_credentials': ''}
    config['directories'] = {}
    with open("backfile.yaml", "w") as file:
        yaml.dump(config, file, default_flow_style=False)

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
        print(f'\nSet {path} as the authentication key file.\n')
    config['authentication_file'] = path
    with open("backfile.yaml", "w") as file:
        yaml.dump(config, file, default_flow_style=False)

@set.command()
@click.pass_context
@click.argument('names', nargs=-1, required=True)
def bucket(ctx, names):
    config = ctx.obj['config']
    if len(config['directories']) == 0:
        print("Cannot set destination bucket as no directories are specified.")
        print("Please add directories for tracking by running `backman add <directory>:<subdirectory>`")
        sys.exit(1)
    for address in names:
        if ':' not in address or len(address.split(':')) > 2:
            print("Usage: backman set bucket <directory>:<bucket>")
            sys.exit(1)
        directory, bucket_addr = address.split(":")
        if directory == "*":
            for dir in config['directories']:
                config['directories'][dir]['bucket'] = bucket_addr
        else:
            if directory not in config['directories']:
                print(f"Directory {directory} not found in Backfile! Please add it with `backman add {directory}:<subdirectory>`")
                sys.exit(1)
            config['directories'][directory]['bucket'] = bucket_addr
        print(f'Set the destination bucket for {directory} to {bucket_addr}')

    # dump the updated config dictionary contents into the backfile
    with open("backfile.yaml", "w") as file:
        yaml.dump(config, file, default_flow_style=False)


@cli.command()
@click.pass_context
def config(ctx):
    config = ctx.obj['config']
    sheet_url = config['google_sheet']['sheet_url']
    sheet_creds = config['google_sheet']['sheet_credentials']

    if sheet_url.strip() != '':
        print('\n============= GOOGLE SHEET SUMMARY =============')
        _, df, _ = retrieve_google_sheet(sheet_url, sheet_creds)
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

        if 'NO' in df['Tracked']:
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
                dirs.append(line)

    added_dirs = {}
    add_bucket = []
    for dir in dirs:
        if ':' in dir:
            if len(dir.split(':')) != 2:
                print('Please provide subdirectories as a list of [directory]:[subdirectory] pairs')
                sys.exit(1)

            directory, subdirectory = dir.split(':')
            if not pathlib.Path(directory).is_dir():
                print(f'{directory} is not a directory!')
                sys.exit(1)

            if subdirectory == '*':
                subdirs = [d for d in pathlib.Path(directory).glob('*') if d.is_dir()]
                if directory not in added_dirs:
                    added_dirs[directory] = []
                if directory not in config['directories']:
                    config['directories'][directory] = {'subdirs': [], 'active': True, 'bucket': ''}
                    add_bucket.append(directory)
                for subdir in subdirs:
                    if 'subdirs' not in config['directories'][directory]:
                        config['directories'][directory]['subdirs'] = [subdir]
                        add_bucket[directory].append(subdir)
                    if subdir not in config['directories'][directory]['subdirs']:
                        config['directories'][directory]['subdirs'].append(subdir)
                        add_bucket[directory].append(subdir)

            if not (pathlib.Path(directory) / subdirectory).is_dir():
                print(f'{subdirectory} is not a directory!')
                sys.exit(1)

            if directory in config['directories']:
                if not 'subdirs' in config['directories'][directory]:
                    config['directories'][directory]['subdirs'] = []
                if len(config['directories'][directory]) == 0:
                    config['directories'][directory]['subdirs'] = subdirectory
                else:
                    config['directories'][directory]['subdirs'].append(subdirectory)
                    if directory in added_dirs:
                        added_dirs[directory].append(subdirectory)
                    else:
                        added_dirs[directory] = [subdirectory]
            else:
                config['directories'][directory] = {'subdirs': [subdirectory]}
                added_dirs[directory] = [subdirectory]

        else:
            directory = dir[:-1] if dir.endswith('/') else dir
            if not pathlib.Path(dir).is_dir():
                print(f'{dir} is not a directory!')
                sys.exit(1)
            if dir in config['directories']:
                print(f'{dir} is already being tracked!')
                sys.exit(1)
            config['directories'][directory] = {}
            if not directory in added_dirs:
                added_dirs[dir] = []

    for directory in added_dirs:
        config['directories'][directory]['active'] = True
        if 'subdirs' not in config['directories'][directory]:
            config['directories'][directory]['subdirs'] = []
    
    with open("backfile.yaml", "w") as f:
        yaml.dump(config, f, default_flow_style=False)
    
    if any([len(added_dirs[dir]) > 0 for dir in added_dirs]):
        print('\nThe following directories have been added to tracking:\n')
        for dir in added_dirs:
            if len(added_dirs[dir]) > 0:
                print(f'{dir}')
                for subdir in added_dirs[dir]:
                    print(f'  - {subdir}')
        print()
    else:
        print('Nothing to add! (All directories/subdirectories already present)')


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

    with open("backfile.yaml", "w") as f:
        yaml.safe_dump(config, f, default_flow_style=False)


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
    with open("backfile.yaml", "w") as f:
        yaml.dump(config, f, default_flow_style=False)


@cli.command()
@click.pass_context
def verify(ctx):
    """Verify the integrity of uploaded files by computing checksums and comparing them with stored checksums in GCS"""
    target_directories = ctx.obj["directories"]
    config = ctx.obj["config"]
    client = ctx.obj["client"]
    sheet_url = config['google_sheet']['sheet_url']
    sheet_creds = config['google_sheet']['sheet_credentials']
    mismatched = {}
    total_mismatched = 0

    # if backman is synced with a Google Sheet, read directory information from it
    if sheet_url != '':
        _, _, target_directories = retrieve_google_sheet(sheet_url, sheet_creds)
    
    print('Computing CRC32c checksums of each file. This may take a while...')
    # iterate over tracked directories and check for outdated/missing files
    for directory in target_directories.keys():
        print(f'Scanning {directory}...')
        if not target_directories[directory]['active']:
            continue
        target_bucket = target_directories[directory]['bucket']
        target_subdirs = target_directories[directory]['subdirs']
        rel_directory = os.path.basename(directory)
        counter = 1

        for subdir in target_subdirs:
            with console.status(f"[bold cyan][{subdir}][/bold cyan] Fetching manifest...", spinner="dots"):
                items, _ = collect_files(directory, subdir)
                gcp_items = retrieve_gcp_files(client, target_bucket, rel_directory, subdir)

            failed_verification = {'missing': [], 'mismatch': []}

            with Progress(
                SpinnerColumn(),
                TextColumn("[bold]{task.description}"),
                BarColumn(),
                MofNCompleteColumn(),
                TextColumn("•"),
                TimeElapsedColumn(),
                console=console,
            ) as progress:
                task = progress.add_task(
                    f"[{counter}/{len(target_subdirs)}] Verifying [bold]{subdir}[/bold]...",
                    total=len(items),
                )

                for file in items:
                    abs_path = file['path']
                    rel_path = os.path.relpath(abs_path, directory)
                    folder = os.path.basename(directory)
                    remote_key = folder + '/' + rel_path

                    if remote_key not in gcp_items:
                        failed_verification['missing'].append(file)
                        total_mismatched += 1
                        progress.advance(task)
                        continue

                    file_crc32c = file_crc32c_b64(abs_path)
                    if file_crc32c != gcp_items[remote_key]["crc32c"]:
                        total_mismatched += 1
                        failed_verification['mismatch'].append(file)

                    progress.advance(task)

            if len(failed_verification['mismatch']) > 0 or len(failed_verification['missing']) > 0:
                mismatched[subdir] = failed_verification
            counter += 1

    # alert the user to any files with mismatches and offer to reupload them
    if len(mismatched) > 0:
        if total_mismatched % 10 == 1:
            placeholder = "ITEM"
        else:
            placeholder = "ITEMS"

        print(f"\n======= {total_mismatched} {placeholder} FAILED VERIFICATION =======\n")
        if total_mismatched > 20:
            # print compressed subdirectory overview
            opt = prompt_choice(f"Print all {total_mismatched} items? (y/[n]): ", ['yes', 'y', 'no', 'n', ''])
            if opt in ['no', 'n', '']:
                print("Displaying summary of tracked directories:")
                # iterate over directories with missing/outdated files and print how many files need to be updated
                for dir in mismatched:
                    mismatch = len(mismatched[dir]['mismatch'])
                    missing = len(mismatched[dir]['missing'])
                    print(f'- {dir}: {len(mismatched[dir])} files out of date')
                    if mismatch > 0:
                        print(f'  • {mismatch} files with checksum mismatch')
                    if missing > 0:
                        print(f'  • {missing} files missing')
            else:
                # print every file + reason for backing up
                for dir in mismatched:
                    print(f"{dir}:")
                    if len(mismatched[dir]['mismatch']) > 0:
                        print(" - checksum mismatch:")
                        for file in mismatched[dir]['mismatch']:
                            print(f"  • {file['path']}")
                    if len(mismatched[dir]['missing']) > 0:
                        print(" - missing:")
                        for file in mismatched[dir]['missing']:
                            print(f"  • {file['path']}")
        else:
            for dir in mismatched:
                print(f"{dir}:")
                if len(mismatched[dir]['mismatch']) > 0:
                    print(" - checksum mismatch:")
                    for file in mismatched[dir]['mismatch']:
                        print(f"  • {file['path']}")
                if len(mismatched[dir]['missing']) > 0:
                    print(" - missing:")
                    for file in mismatched[dir]['missing']:
                        print(f"  • {file['path']}")
    
        resp = prompt_choice('Would you like to run `backman update` automatically to upload the mismatched/missing files? (y/[n]): ', ['y', 'yes', 'n', 'no', ''])
        if resp in ['y', 'yes']:
            update(ctx)

        print()
        sys.exit(0)
    else:
        print('All checksums matched!\n')
        sys.exit(0)


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
    
    def _download_blobs(blobs, dest_dir, progress, task):
        dest = pathlib.Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)

        for blob in blobs:
            out = dest / blob.name
            out.parent.mkdir(parents=True, exist_ok=True)  # preserve subdirs
            blob.download_to_filename(out)
            progress.advance(task)


    target_directories = ctx.obj["directories"]
    config = ctx.obj["config"]
    client = ctx.obj["client"]
    sheet_url = config['google_sheet']['sheet_url']
    sheet_creds = config['google_sheet']['sheet_credentials']
    dirs_to_restore = {}
    total_size = 0

    # if backman is synced with a Google Sheet, read directory information from it
    if sheet_url != '':
        _, _, target_directories = retrieve_google_sheet(sheet_url, sheet_creds)

    if len(dirs) == 0:
        print('Usage: backman restore --dirs [directory1, directory1:subdirectory]')

    for dir in dirs:
        if dir == '*':
            for directory in target_directories.keys():
                dirs_to_restore[directory] = target_directories[directory]['subdirs']

        if ':' in dir:
            if len(dir.split(':')) != 2:
                print('Please provide subdirectories as a list of [directory]:[subdirectory] pairs')
                sys.exit(1)
            directory, subdirectory = dir.split(':')
            if not pathlib.Path(directory).is_dir():
                print(f'{directory} is not a directory!')
                sys.exit(1)
            if subdirectory == '*':
                dirs_to_restore[directory] = target_directories[directory]['subdirs']
                continue
            if directory in dirs_to_restore:
                dirs_to_restore[directory].append(subdirectory)
            else:
                dirs_to_restore[directory] = [subdirectory]

        else:
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
                gcp_items = retrieve_gcp_files(client, target_bucket, rel_directory, subdir, return_blobs=False)
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

    for dir in dirs_to_restore:
        current_dirs = [p for p in pathlib.Path.cwd().iterdir() if p.is_dir()]
        rel_directory = os.path.basename(dir)
        target_bucket = target_directories[dir]['bucket']
        if dir in current_dirs:
            dirname = dir + '_backup'
            counter = 0
            while dirname in current_dirs:
                dirname += str(counter)
        else:
            dirname = dir

        pathlib.Path(dirname).mkdir()

        for subdir in dirs_to_restore[dir]:
            subdir_dest = pathlib.Path(dirname) / subdir
            subdir_dest.mkdir()

            blobs = list(retrieve_gcp_files(client, target_bucket, rel_directory, subdir, return_blobs=True))
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
                _download_blobs(blobs, subdir_dest, progress, task)

    console.print("[green]✓[/green] All backups restored.")


@cli.command()
@click.pass_context
def history(ctx):
    # TODO
    # incorporate history doc writing
    # read and display history doc

    pass


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

    cron = CronTab(user=True)
    
    # Define the command (use sys.executable to ensure the same Python env is used)
    # Use absolute paths for your script!
    script_path = pathlib.Path(__file__).resolve()
    command = f"{sys.executable} {script_path} update >> {script_path}.log 2>&1"
    
    # Create a new job (avoid duplicates by checking for a unique comment)
    job_comment = "regular_backman_job"
    cron.remove_all(comment=job_comment) # Clean up old versions
    
    job = cron.new(command=command, comment=job_comment)
    
    # Set the schedule (SemVer-style frequency)
    job.setall(cron_string)
    
    # Write to the system crontab
    cron.write()
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
        iter = croniter(schedule, now)
        next_run = iter.get_next(datetime)
        print(f"Next scheduled run: {next_run.strftime('%Y-%m-%d %H:%M:%S')}")
    else:
        print("No cron jobs scheduled!")


if __name__ == "__main__":
    cli()


    
