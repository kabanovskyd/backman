#!/usr/bin/env python3

import yaml
import os
import pathlib
import hashlib
import sys
import click

from google.cloud import storage


EXCLUDE_EXTENSIONS = {
    ".pyc",
    ".pyo",
    ".json",
    ".env",
    ".bam",
    ".swp",   # vim swap files
}

EXCLUDE_DIRS = {
    ".git",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    "env",
    "tmp",
    "temp",
    "scratch",
}


def prompt_choice(prompt, valid_options):
    while True:
        response = input(prompt).strip().lower()
        if response in valid_options:
            return response
        print(f"Invalid input. Valid options are: {', '.join(valid_options)}")


def upload(
    bucket: storage.Bucket,
    local_path: str,
    remote_path: str
) -> None:
    stat = os.stat(local_path)
    if not bucket.exists():
        print(f'Bucket {bucket} does not exist')
        exit(1)
    blob = bucket.blob(remote_path)
    blob.metadata = {
        "source_mtime": str(stat.st_mtime),
        "source_size": str(stat.st_size),
        "source_path": local_path,
    }
    blob.upload_from_filename(local_path)


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
        remote_key = bucket_prefix + folder + rel_path
        # print(remote_key)

        if remote_key not in remote_manifest:
            to_upload.append({**file, "reason": "missing"})
        elif file["size"] != remote_manifest[remote_key]["size"]:
            to_upload.append({**file, "reason": "size_changed"})
        # optionally add MD5 comparison here for extra confidence

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
            "md5": blob.md5_hash,  # base64-encoded MD5
        }

    return manifest


def collect_files(root: str, subdir: str) -> list[dict]:
    results = []
    # print(f'Processing directory: {root}/{subdir}...')
    def _walk(path):
        with os.scandir(path) as it:
            for entry in it:
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
                        })

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
    with open('config.yaml', 'r') as file:
        config = yaml.safe_load(file)
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "./credentials.json"
    ctx.ensure_object(dict)
    ctx.obj["config"] = config
    ctx.obj["target_dirs"] = config['target_dirs']
    ctx.obj["target_bucket"] = config['target_bucket']
    ctx.obj["client"] = storage.Client()


@cli.command()
@click.pass_context
def status(ctx):
    upload_dict = {}
    total_items = 0
    target_directories = ctx.obj["target_dirs"]
    config = ctx.obj["config"]
    client = ctx.obj["client"]
    target_bucket = ctx.obj["target_bucket"]
    for directory in target_directories.keys():
        target_subdirs = config['target_dirs'][directory]
        for subdir in target_subdirs:
            items = collect_files(directory, subdir)
            rel_directory = directory.split('/')[-1]
            gcp_items = retrieve_gcp_files(client, target_bucket, rel_directory, subdir)
            print(gcp_items)
            to_upload = find_files_to_upload(items, gcp_items, f"{rel_directory}/{subdir}/", directory)
            if len(to_upload) > 0:
                upload_dict[subdir] = to_upload
                total_items += len(to_upload)

    if len(upload_dict) > 0:
        print("======= OUTDATED ITEMS =======")
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
                    print(dir)
                    for file in upload_dict[dir]:
                        print(f"- {file['path']} ({file['reason']})")
        exit(0)
    else:
        print('Everything up to date')
        exit(0)


@cli.command()
@click.pass_context
def update(ctx):
    """Run the backup on directories specified in the config file, updating any out-of-date files."""
    target_directories = ctx.obj["target_dirs"]
    config = ctx.obj["config"]
    client = ctx.obj["client"]
    target_bucket = ctx.obj["target_bucket"]
    for directory in target_directories.keys():
        target_subdirs = config['target_dirs'][directory]
        for subdir in target_subdirs:
            items = collect_files(directory, subdir)
            rel_directory = directory.split('/')[-1]
            gcp_items = retrieve_gcp_files(client, target_bucket, rel_directory, subdir)
            to_upload = find_files_to_upload(items, gcp_items, f"{rel_directory}/{subdir}/", directory)
            for item in to_upload:
                path = item['path']
                rel_path = path.split(directory)[-1]
                bucket = f'{rel_directory}{rel_path}'
                print(bucket)
                bucket_handle = client.bucket(target_bucket)
                upload(bucket_handle, item['path'], bucket)


@cli.command()
@click.pass_context
@click.argument("dirs", nargs=-1, required=True)
def exclude(dirs):
    """Exclude specified directories from future backups, but keep them in the config file."""
    pass


if __name__ == "__main__":
    cli()


    
