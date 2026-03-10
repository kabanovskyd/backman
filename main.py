import yaml
import os
import pathlib
import hashlib

from google.cloud import storage


EXCLUDE_EXTENSIONS = {
    ".pyc",
    ".pyo",
    ".json",
    ".env",
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


def upload(
    bucket: storage.Bucket,
    local_path: str,
    remote_path: str
) -> None:
    stat = os.stat(local_path)
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
    bucket_prefix: str
) -> list[dict]:

    """Return only local files that are missing or changed in GCS."""
    to_upload = []

    for file in local_files:
        remote_key = bucket_prefix + file["relative_path"]

        if remote_key not in remote_manifest:
            to_upload.append({**file, "reason": "missing"})
        elif file["size"] != remote_manifest[remote_key]["size"]:
            to_upload.append({**file, "reason": "size_changed"})
        # optionally add MD5 comparison here for extra confidence

    print('>>>>>> OUTDATED FILES <<<<<<')
    for file in to_upload:
        print(file)

    return to_upload


def retrieve_gcp_files(
    client,
    bucket,
    directory,
    subdir
) -> dict:
    blobs = client.list_blobs(bucket, prefix=f"{directory}/{subdir}/")
    manifest = {}

    print('Scanning bucket: ')
    for blob in blobs:
        manifest[blob.name] = {
            "size": blob.size,
            "updated": blob.updated,
            "md5": blob.md5_hash,  # base64-encoded MD5
        }
    print('Found contents: ')
    for blob in blobs:
        print(f'- {blob.name}')

    return manifest


def collect_files(root: str) -> list[dict]:
    results = []
    print(f'Processing directory: {root}...')
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

    _walk(root)
    print(f'Found contents:')
    for item in results:
        print(f'- {root}/{item}')

    return results

os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "./credentials.json"
client = storage.Client()

with open('config.yaml', 'r') as file:
    config = yaml.safe_load(file)

target_directories = config['target_dirs']
target_bucket = config['target_bucket']

for directory in target_directories.keys():
    target_subdirs = config['target_dirs'][directory]
    for subdir in target_subdirs:
        items = collect_files(subdir)
        exit()
        directory = directory.split('/')[-1]
        gcp_items = retrieve_gcp_files(directory, subdir)
        to_upload = find_files_to_upload(items, gcp_items, f"{directory}/{subdir}/")
        for item in to_upload.keys():
            bucket = target_bucket + f'/{directory}/{subdir}'
            upload(bucket, item, bucket + '/' + item)





    
