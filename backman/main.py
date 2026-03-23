#!/usr/bin/env python3

import sys
import json
import shlex
import subprocess
import tempfile
import yaml
import os
import pathlib
import math
import hashlib
import queue
import threading
import click
import crcmod
import struct
import base64

from google.cloud import storage
from google.cloud.storage import transfer_manager


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


def prompt_choice(prompt, valid_options):
    while True:
        response = input(prompt).strip().lower()
        if response in valid_options:
            return response
        print(f"Invalid input. Valid options are: {', '.join(valid_options)}")


def crc32c(filepath):
    crc_fn = crcmod.predefined.mkCrcFun('crc-32c')
    with open(filepath, 'rb') as f:
        crc = crc_fn(f.read())
    # GCP returns CRC32c as base64-encoded big-endian 32-bit int
    return base64.b64encode(struct.pack('>I', crc)).decode('utf-8')


def upload_parallel(bucket_name, items, directory, rel_directory, max_workers, client=None):
    bucket = client.bucket(bucket_name)

    # lower the threshold so fewer large files are called "small"
    EFFECTIVE_SMALL_THRESHOLD = 50 * 1024 * 1024  # 50 MB

    small = [f for f in items if f["size"] < EFFECTIVE_SMALL_THRESHOLD]
    large = [f for f in items if f["size"] >= EFFECTIVE_SMALL_THRESHOLD]

    # process small files in batches rather than all at once
    BATCH_SIZE = 50
    if small:
        total = len(small)
        n_batches = math.ceil(total / BATCH_SIZE)
        print(f"  Uploading {total} small file(s) in {n_batches} batch(es)...", flush=True)

        for batch_idx in range(n_batches):
            batch = small[batch_idx * BATCH_SIZE : (batch_idx + 1) * BATCH_SIZE]

            # only build the list for this batch, not all files
            file_blob_pairs = [
                (
                    item["path"],
                    bucket.blob(f"{rel_directory}{item['path'].split(directory)[-1]}")
                )
                for item in batch
            ]

            results = transfer_manager.upload_many(
                file_blob_pairs,
                worker_type="thread",
                max_workers=min(max_workers, 4),
            )

            for (local_path, blob), result in zip(file_blob_pairs, results):
                if isinstance(result, Exception):
                    print(
                        f"  ERROR {local_path}: "
                        f"{type(result).__name__}: {result or '(no message)'}",
                        flush=True
                    )
                else:
                    print(f"  Uploaded gs://{bucket_name}/{blob.name}", flush=True)

            # explicitly release memory before next batch
            del file_blob_pairs
            del results

    if large:
        print(f"  Uploading {len(large)} large file(s) with chunked streaming...", flush=True)
        for item in large:
            rel_path = item["path"].split(directory)[-1]
            remote_key = f"{rel_directory}{rel_path}"
            blob = bucket.blob(remote_key)
            try:
                transfer_manager.upload_chunks_concurrently(
                    item["path"],
                    blob,
                    chunk_size=256 * 1024 * 1024,
                    max_workers=4,
                )
                print(f"  Uploaded gs://{bucket_name}/{blob.name}", flush=True)
            except Exception as e:
                print(f"  ERROR {item['path']}: {e}", flush=True)


def submit_uger_job(
    bucket_name: str,
    items: list[dict],
    directory: str,
    rel_directory: str,
    jobs: int,
    credentials_path: str,
) -> None:
    """Write a JSON manifest and qsub array job script, then submit via qsub."""
    work_dir = tempfile.mkdtemp(prefix="backman_uger_")
    os.makedirs(os.path.join(work_dir, "logs"), exist_ok=True)

    manifest = []
    for item in items:
        local_path = item["path"]
        rel_path = local_path.split(directory)[-1]
        remote_key = rel_directory + rel_path
        stat = os.stat(local_path)
        manifest.append({
            "local_path": local_path,
            "remote_key": remote_key,
            "source_mtime": str(stat.st_mtime),
            "source_size": str(stat.st_size),
            "bucket": bucket_name,
        })

    manifest_path = os.path.join(work_dir, "manifest.json")
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    helper_path = os.path.join(work_dir, "upload_task.py")
    helper_script = f"""\
    #!{sys.executable}
    import json, os, sys
    from google.cloud import storage
    from google.cloud.storage import transfer_manager

    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = {repr(credentials_path)}
    manifest_path = {repr(manifest_path)}
    task_index = int(os.environ["SGE_TASK_ID"]) - 1

    with open(manifest_path) as f:
        manifest = json.load(f)

    if task_index >= len(manifest):
        sys.exit(0)

    task = manifest[task_index]
    client = storage.Client()
    bucket = client.bucket(task["bucket"])
    blob = bucket.blob(task["remote_key"])
    blob.content_type = "application/gzip"
    blob.metadata = {{
        "source_mtime": task["source_mtime"],
        "source_size": task["source_size"],
        "source_path": task["local_path"],
    }}

    # Use chunked upload instead of upload_from_filename
    transfer_manager.upload_chunks_concurrently(
        task["local_path"],
        blob,
        chunk_size=256 * 1024 * 1024,  # 256 MB chunks
        max_workers=4,
    )
    print(f"Uploaded {{task['local_path']}} -> gs://{{task['bucket']}}/{{task['remote_key']}}")
    """
    with open(helper_path, "w") as f:
        f.write(helper_script)

    print(f"  Manifest: {manifest_path}")
    print(f"  Job script: {helper_path}")
    print(f"  Submitting task(s), max {jobs} concurrent...")

    result = subprocess.run(["qsub", helper_path], capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  qsub failed:\n{result.stderr}")
        sys.exit(1)
    print(f"  {result.stdout.strip()}")


def find_files_to_upload(
    local_files: list[dict],
    remote_manifest: dict,
    bucket_prefix: str,
    directory: str
) -> list[dict]:

    """Return only local files that are missing or changed in GCS."""
    to_upload = []

    for file in local_files:
        abs_path = file['path']
        rel_path = abs_path.split(directory)[-1]
        folder = directory.split('/')[-1]
        remote_key = folder + rel_path

        if remote_key not in remote_manifest:
            to_upload.append({**file, "reason": "missing"})
        elif file["size"] != remote_manifest[remote_key]["size"]:
            to_upload.append({**file, "reason": "modified"})
        elif file["crc32c"] != remote_manifest[remote_key]["crc32c"]:
            to_upload.append({**file, "reason": "checksum mismatch"})

    return to_upload


def retrieve_gcp_files(
    client,
    bucket,
    directory,
    subdir
) -> dict:
    blobs = client.list_blobs(bucket, prefix=f"{directory}/{subdir}/")
    manifest = {}

    # print('Scanning bucket: ')
    for blob in blobs:
        # print(f'- {blob.name}')
        manifest[blob.name] = {
            "size": blob.size,
            "updated": blob.updated,
            "crc32c": blob.crc32c,  # base64-encoded CRC32c
        }

    return manifest


def collect_files(root: str, subdir: str) -> list[dict]:
    results = []
    # print(f'Processing directory: {root}/{subdir}...')
    def _walk(path):
        try:
            entries = list(os.scandir(path))
        except PermissionError:
            print(f"Warning: permission denied, skipping {path}")
            return
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name not in EXCLUDE_DIRS:
                        _walk(entry.path)
                elif entry.is_file(follow_symlinks=False):
                    ext = os.path.splitext(entry.name)[1].lower()
                    if ext not in EXCLUDE_EXTENSIONS:
                        stat = entry.stat()
                        results.append({
                            "path": entry.path,
                            "size": stat.st_size,
                            "mtime": stat.st_mtime,
                            "crc32c": crc32c(entry.path)
                        })
            except PermissionError:
                print(f"Warning: permission denied, skipping {entry.path}")

    path = pathlib.Path(root) / subdir
    _walk(path)
    #print(f'Found contents:')
    #for item in results:
    #    print(f'- {root}/{item}')

    return results
'''
print("""
      ------------------------------------------------------------------
      ██████╗  █████╗  ██████╗██╗  ██╗     ███╗   ███╗ █████╗ ███╗   ██╗    
      ██╔══██╗██╔══██╗██╔════╝██║ ██╔╝     ████╗ ████║██╔══██╗████╗  ██║   
      ██████╔╝███████║██║     █████╔╝█████╗██╔████╔██║███████║██╔██╗ ██║  
      ██╔══██╗██╔══██║██║     ██╔═██╗╚════╝██║╚██╔╝██║██╔══██║██║╚██╗██║  
      ██████╔╝██║  ██║╚██████╗██║  ██╗     ██║ ╚═╝ ██║██║  ██║██║ ╚████║  
      ╚═════╝ ╚═╝  ╚═╝ ╚═════╝╚═╝  ╚═╝     ╚═╝     ╚═╝╚═╝  ╚═╝╚═╝  ╚═══╝
      ------------------------------------------------------------------
      """)
'''

@click.group()
@click.pass_context
def cli(ctx):
    """backman — automated lab data backup tool."""
    ctx.ensure_object(dict)
    
    # skip config loading if init is being called
    if ctx.invoked_subcommand == "init":
        return

    with open('backfile.yaml', 'r') as file:
        config = yaml.safe_load(file)

    ctx.obj["config"] = config
    if ctx.invoked_subcommand == "set-auth":
        return

    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "./credentials.json"
    if 'directories' not in config:
        print('No directories specified for tracking! Run `backman add [directories]` to track and update directories.')
        exit(1)
    
    ctx.obj["directories"] = config['directories']
    try:
        ctx.obj["client"] = storage.Client()
    except Exception as e:
        print(f"Could not establish a connection to GCP: {e}")
        exit(1)


@cli.command()
@click.pass_context
def status(ctx):
    upload_dict = {}
    total_items = 0
    target_directories = ctx.obj["directories"]
    config = ctx.obj["config"]
    client = ctx.obj["client"]
    for directory in target_directories.keys():
        if not target_directories[directory]['active']:
            continue
        target_bucket = target_directories[directory]['bucket']
        target_subdirs = target_directories[directory]['subdirs']
        for subdir in target_subdirs:
            items = collect_files(directory, subdir)
            rel_directory = directory.split('/')[-1]
            gcp_items = retrieve_gcp_files(client, target_bucket, rel_directory, subdir)
            to_upload = find_files_to_upload(items, gcp_items, f"{rel_directory}/{subdir}/", directory)
            if len(to_upload) > 0:
                upload_dict[subdir] = to_upload
                total_items += len(to_upload)

    if len(upload_dict) > 0:
        print("\n======= OUTDATED ITEMS =======\n")
        if total_items > 20:
            opt = prompt_choice(f"Print all {len(to_upload)} items? (y or n): ", ['yes', 'y', 'no', 'n'])
            if opt in ['no', 'n']:
                print("Displaying summary of tracked directories:")
                for dir in upload_dict:
                    modified = len([file for file in upload_dict[dir] if file['reason'] == 'modified'])
                    missing = len([file for file in upload_dict[dir] if file['reason'] == 'missing'])
                    print(f'- {dir}: {len(upload_dict[dir])} files out of date')
                    if modified > 0:
                        print(f'  • {modified} modified')
                    if missing > 0:
                        print(f'  • {missing} missing')
            else:
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
        exit(0)
    else:
        print('Everything up to date!\n')
        exit(0)


@cli.command()
@click.pass_context
@click.option("--jobs", default=4, show_default=True, help="Parallel upload workers (local) or max concurrent UGER tasks.")
@click.option("--uger", is_flag=True, default=False, help="Submit uploads as a UGER qsub array job instead of uploading directly.")
def update(ctx, jobs, uger):
    """Run the backup on directories specified in the config file, updating any out-of-date files."""
    target_directories = ctx.obj["directories"]
    config = ctx.obj["config"]
    client = ctx.obj["client"]
    credentials_path = os.path.abspath(config.get("authentication_file", "./credentials.json"))

    for directory in target_directories.keys():
        if not target_directories[directory].get("active", True):
            continue
        target_subdirs = target_directories[directory]["subdirs"]
        target_bucket = target_directories[directory]["bucket"]
        rel_directory = directory.split("/")[-1]

        for subdir in target_subdirs:
            items = collect_files(directory, subdir)
            gcp_items = retrieve_gcp_files(client, target_bucket, rel_directory, subdir)
            to_upload = find_files_to_upload(items, gcp_items, f"{rel_directory}/{subdir}/", directory)

            if not to_upload:
                print(f"[{subdir}] Nothing to upload.", flush=True)
                continue

            print(f"[{subdir}] {len(to_upload)} file(s) to upload.", flush=True)

            if uger:
                submit_uger_job(
                    bucket_name=target_bucket,
                    items=to_upload,
                    directory=directory,
                    rel_directory=rel_directory,
                    jobs=jobs,
                    credentials_path=credentials_path,
                )
            else:
                upload_parallel(
                    bucket_name=target_bucket,
                    items=to_upload,
                    directory=directory,
                    rel_directory=rel_directory,
                    max_workers=jobs,
                    client=client,
                )


@cli.command()
@click.pass_context
@click.argument("dirs", nargs=-1, required=True)
def exclude(ctx, dirs):
    """Exclude specified directories from future backups, but keep them in the config file."""
    config = ctx.obj["config"]

    if any(directory not in config['directories'] for directory in dirs):
        print('\nThe following directories are not present in the backfile:\n')
        for directory in dirs:
            if directory not in config['directories']:
                print(f'- {directory}')
        print('\nPlease make sure all listed directories are present in the backfile and re-run the command.\n')
        exit(1)

    for directory in dirs:
        config['directories'][directory]['active'] = False
    
    with open("backfile.yaml", "w") as f:
        yaml.dump(config, f, default_flow_style=False)
    
    print('\nThe following directories have been excluded from tracking:\n')
    for dir in dirs:
        print(f'- {dir}')
    print()


@cli.command()
@click.pass_context
@click.argument("dirs", nargs=-1, required=True)
def include(ctx, dirs):
    config = ctx.obj["config"]

    if any(directory not in config['directories'] for directory in dirs):
        print('\nThe following directories are not present in the backfile:\n')
        for directory in dirs:
            if directory not in config['directories']:
                print(f'- {directory}')
        print('\nPlease make sure all listed directories are present in the backfile and re-run the command.\n')
        exit(1)

    for directory in dirs:
        config['directories'][directory]['active'] = True
    
    with open("backfile.yaml", "w") as f:
        yaml.dump(config, f, default_flow_style=False)
    
    print('\nThe following directories have been included in tracking:\n')
    for dir in dirs:
        print(f'- {dir}')
    print()


@cli.command()
def init():
    print()
    if pathlib.Path('./backfile.yaml').is_file():
        print('WARNING: you are about to overwrite the existing Backfile - this will delete ALL data about currently tracked directories!')
        opt = prompt_choice('Are you sure you would like to continue? (y/n): ', ['yes', 'y', 'no', 'n'])
        if opt not in ['yes', 'y']:
            exit(0)

    print('Creating Backfile...')
    config = {}
    config['authentication_file'] = ''
    config['directories'] = {}
    with open("backfile.yaml", "w") as file:
        yaml.dump(config, file, default_flow_style=False)

    print('Backfile created! Please run `backman set-auth [authentication_file]` to provide backman with a valid JSON authentication key file for GCP access.')


@cli.command()
@click.pass_context
@click.argument("auth_key_path", nargs=1, required=True)
def set_auth(ctx, auth_key_path):
    config = ctx.obj['config']
    if not pathlib.Path(auth_key_path).is_file():
        print(f'{auth_key_path} not found.')
        exit(1)

    print(f'\nSet {auth_key_path} as the authentication key file.\n')
    config['authentication_file'] = auth_key_path
    with open("backfile.yaml", "w") as file:
        yaml.dump(config, file, default_flow_style=False)


@cli.command()
@click.pass_context
def config(ctx):
    config = ctx.obj['config']
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


@cli.command()
@click.pass_context
@click.argument("dirs", nargs=-1, required=True)
def add(ctx, dirs):
    config = ctx.obj["config"]

    if dirs[0] == '--file':
        dir_file = dirs[1]
        if len(dirs) > 2:
            print('Usage: backman add --file [file_with_directories]')
            exit(1)
        if not pathlib.Path(dir_file).is_file():
            print(f'File {dir_file} does not exist!')
            exit(1)
        
        dirs = []
        with open(dir_file, 'r') as file:
            for line in file:
                dirs.append(line)

    added_dirs = {}
    for dir in dirs:
        if ':' in dir:
            if len(dir.split(':')) != 2:
                print('Please provide subdirectories as a list of [directory]:[subdirectory] pairs')
                exit(1)
            directory, subdirectory = dir.split(':')
            if not pathlib.Path(directory).is_dir():
                print(f'{directory} is not a directory!')
                exit(1)
            if subdirectory == '*':
                #TODO: implement globbing logic
                pass
            if not (pathlib.Path(directory) / subdirectory).is_dir():
                print(f'{subdirectory} is not a directory!')
                exit(1)
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
                exit(1)
            if dir in config['directories']:
                print(f'{dir} is already being tracked!')
                exit(1)
            config['directories'][directory] = {}
            if not directory in added_dirs:
                added_dirs[dir] = []

    for directory in added_dirs:
        config['directories'][directory]['active'] = True
        if 'subdirs' not in config['directories'][directory]:
            config['directories'][directory]['subdirs'] = []
    
    with open("backfile.yaml", "w") as f:
        yaml.dump(config, f, default_flow_style=False)
    
    print('\nThe following directories have been added to tracking:\n')
    for dir in added_dirs:
        print(f'{dir}')
        if len(added_dirs[dir]) > 0:
            for subdir in added_dirs[dir]:
                print(f'  - {subdir}')
    print()


if __name__ == "__main__":
    cli()


    
