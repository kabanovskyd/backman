![Awesome logo](backman-logo.png)
# backman #
A command-line tool for managing and automating lab data backups to Google Cloud Storage (GCS).

### Requirements ###

- A Google Cloud Storage bucket
- A GCP service account credentials JSON file with Storage Object Admin (or higher) permissions

The installation script `backman-installer.sh` automatically downloads and installs `uv`, which is used for dependency/environment management. This means that no manual package installations are needed!

### Installation ###
```bash
# clone the Git repo
git clone https://github.com/kabanovskyd/backman.git
cd backman

# run the backman installer
bash backman-installer.sh

# reload the bashrc file to export the modified PATH variable
source ~/.bashrc
```
After installation, backman will be available as a system command.

### Setup ###
Initialize a new backman configuration:
```bash
backman init
```
Then set your GCP credentials file:
```bash
backman set auth /path/to/credentials.json
```
The configuration is saved to `backfile.yaml` in the current directory.

### Configuration ###
`backman` stores its configuration in a `backfile.yaml` configuration file (Backfile):
```yaml
authentication_file: /path/to/credentials.json
google_sheet:
  sheet_url: ''
  sheet_credentials: ''
directories:
  /data/lab/project1:
    bucket: backup_archive_1
    active: true
    subdirs:
      - subdirectory1
      - subdirectory2
  /data/lab/project2:
    bucket: backup_archive_2
    active: false
    subdirs:
      - subdirectory1
```
Backfiles can be edited manually, but it is generally recommended to interact with them only through `backman` commands as this is guaranteed to preserve the internal structure of the files required for correct functioning.

### Commands ###

#### Setup ####
- `backman init` — Initialize a new Backfile in the current directory.
    - Note: this will **overwrite** an existing Backfile! Use this only when you want to start from scratch.
- `backman set auth <auth_file>` — Set the GCP credentials JSON file.
- `backman set bucket <dir>:<bucket> ...` — Assign a GCS bucket to a directory. Use `*` to assign to all tracked directories.
- `backman sync <url> <creds>` — Sync directory config from a Google Sheet (overrides Backfile tracking).
- `backman unsync` — Remove Google Sheet sync and revert to Backfile-only tracking.

#### Tracking ####
- `backman add <dir>:<subdir> ...` — Add a directory/subdirectory pair to tracking.
    - `backman add /data/lab/project1:subdir1 /data/lab/project2:subdir2`
    - OR read from a file: `backman add --file dirs.txt`
- `backman exclude <dir> ...` — Pause tracking for specified directories (kept in config, marked inactive).
    - `backman exclude /data/lab/project1 /data/lab/project2`
- `backman include <dir> ...` — Resume tracking for previously excluded directories.
    - `backman include /data/lab/project1`
- `backman config` — Display the current Backfile or Google Sheet configuration.

#### Backup & Restore ####
- `backman status` — Show outdated/missing files across all tracked directories.
- `backman update` — Upload missing or changed files to GCS.
    - `--all` — Re-upload all files regardless of change status.
    - `--jobs <n>` — Number of parallel upload workers (default: 4).
- `backman verify` — Compare local CRC32c checksums against GCS to confirm backup integrity.
- `backman restore <dir> ...` — Download a backup from GCS to local disk.
    - `backman restore /data/lab/project1:subdir1` — Restore a specific subdirectory.
    - `backman restore /data/lab/project1:*` — Restore all subdirectories for a directory.
    - `backman restore *` — Restore all tracked directories.

### Notes ###
- Requires a GCP service account JSON key; set with: `backman set auth <auth_file>`
- Directory format for `add`/`restore`: `/absolute/path/to/dir:subdirname`
- `backfile.yaml` must exist in the working directory for most commands

### GCP Permissions ###
The GCP service account associated with your credentials file requires at minimum:

| Permission | Purpose |
|---|---|
| Storage Object Admin | Upload, download, and delete objects in the bucket |
| Storage Legacy Bucket Reader | Check if the bucket exists |

### Troubleshooting ###

### License ###
MIT
