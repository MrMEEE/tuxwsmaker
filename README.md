# tuxwsmaker

Image factory server for Linux workstation/laptop deployment.

## Current foundation

- Django project scaffold with menu domains: OS, Partition Layouts, Build Config, Package Lists, Builds, Configuration
- Auth base with local + LDAP backend wiring
- Data models for OS/ISO, key/value overrides, partition layouts (including LUKS fields), package lists, build definitions, server config
- OS module with CRUD, multi-ISO upload/delete, OS-level variables, and ISO-level override variables
- Partition Layout module with GUI entry editor and YAML fallback editor (including LUKS fields)
- Build Config module with CRUD for VM runtime settings
- Build Definition module with CRUD, queue action, and artifact listing
- Package Lists module with CRUD and bulk package item editor
- Server Configuration module with CRUD and queue concurrency controls
- Artifact export pipeline for USB raw image output and PXE BIOS/UEFI config bundle output
- Artifact download actions available from the Build detail page
- Celery worker pipeline scaffold
- Build orchestration split:
  - VM lifecycle management via Python libvirt service
  - Post-kickstart guest configuration via ansible-playbook
   - New SSH keypair generated for every build VM; public key is intended for kickstart injection and private key is used for first login and Ansible
   - Builder VM setup is a separate flow from host PXE rollout services.
- Release flow added via release.sh and tools/release.py using version.txt

## Development

1. Create venv and install dependencies:

   python3 -m venv .venv
   . .venv/bin/activate
   pip install -r requirements.txt

2. Copy environment template:

   cp .env.example .env

3. Run migrations and start server:

   python manage.py migrate
   python manage.py runserver

## Virtualization dependency

VM lifecycle code uses Python libvirt bindings. Install OS package for libvirt Python bindings on the build host (for example python3-libvirt on Debian/Ubuntu or libvirt-python on RHEL family).

Kickstart-based VM creation in the worker additionally requires:

- virt-install
- python3-gi (provides Python gi module used by virt-install)
- qemu-img
- pycdlib (Python package, installed via requirements.txt)

## Build SSH key behavior

- Every build generates a unique SSH keypair.
- The key's public part is attached to the build VM definition for kickstart-time injection.
- The private key is used for post-kickstart SSH readiness checks and ansible-playbook access.
- Private key files are cleaned up at task completion.

## Artifact outputs

- USB output: generated from the VM qcow2 disk via qemu-img convert to raw .img
- PXE output: generated as a bundle directory containing extracted kernel/initrd, BIOS and UEFI boot config, and manifest metadata
- Compression: controlled by Server Configuration (enable_artifact_compression)

## Artifact cleanup

Run retention cleanup manually:

python manage.py cleanup_artifacts

The command removes artifact records and files older than configured artifact_retention_days.

## Service management

Manage local test processes with:

./scripts/manage-services.sh <start|stop|restart|status>

Default service set:

- tuxwsmaker-web
- tuxwsmaker-worker
- tuxwsmaker-beat
- redis

Override default services per command:

TUXWS_SERVICES="tuxwsmaker-web tuxwsmaker-worker redis" ./scripts/manage-services.sh restart

The script stores pid files in .run/ and logs in .run/logs/.

## Release

Use release script only:

./release.sh
