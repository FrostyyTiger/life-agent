# Restoring the personal host

Pick the smallest case that applies. The backups hold the data; this repo holds the setup.

| What is lost | Restore from | Steps |
| --- | --- | --- |
| a file or folder on `/data` | ZFS snapshot | `/data/.zfs/snapshot/<snap>/…` on the host dataset; copy back |
| a moved file ("where did it go?") | `~/files-index/moves.log` | `grep <name> moves.log`, reverse the move with `plan-move` |
| one document | Paperless | trash in the UI, or `~/paperless/export/` |
| a GitHub repo | `~/github-mirror/<repo>.git` | `git push --mirror` to a new remote (skill `mirror`) |
| the VM | nightly VM backup (PBS) | restore the VM in Proxmox; everything below comes back with it |
| the archive disk | `personal.pxar` in the host-level backup | case A below |
| the VM and its backups | this repo + the archive disk | case B below |

## A. New archive disk

1. Create the pool on the new disk (on the hypervisor, as root): a raw 32-byte key file, then
   `zpool create -o ashift=12 -O encryption=aes-256-gcm -O keyformat=raw -O keylocation=file:///root/<pool>.key
   -O compression=lz4 -O atime=off -O xattr=sa -O utf8only=on -O normalization=formD
   -O recordsize=1M -O canmount=off -O mountpoint=none -o cachefile=none -o failmode=continue <pool> /dev/disk/by-id/<disk>`, then
   `zfs create -o mountpoint=<MOUNT> <DATASET>`, `chown 1000:1000 <MOUNT>` (and `chattr +i` on the empty mountpoint while unmounted). Store a copy of the key in
   the owner's password manager.
2. `proxmox/install.sh` (needs `/etc/default/personal`), then a Proxmox directory mapping for the VM's
   virtiofs share pointing at `<MOUNT>`.
3. Restore `personal.pxar`: `proxmox-backup-client mount host/<id>/<snapshot> personal.pxar /mnt/r`,
   then `rsync -aHAX /mnt/r/ <MOUNT>/`. (A `restore --pattern` reads the whole archive anyway; mounting
   is faster for partial restores.)

## B. VM from scratch

1. Debian, user with uid 1000, Tailscale (tag the node; the network policy lets it SSH to the build VM
   only), packages: `git docker.io docker-compose-plugin samba exiftool python3 unrar 7zip tmux`.
2. GitHub token (fine-grained, read-only Contents on the owner's repos), typed by the owner:
   `umask 077; read -rsp token: t; printf '%s\n' "$t" > ~/.config/github-mirror.token`.
3. Clone both repos into `~/assistant/` (using the token), then
   `~/assistant/life-agent/host/vm/install.sh --system`.
4. Paperless: `~/paperless/docker-compose.env` and `db.env` from the examples (`chmod 600`),
   `docker compose up -d`, the owner creates the admin (`docker compose exec webserver createsuperuser`),
   then `document_importer ../export` from the last export. Recreate the assistant user + token and
   its no-access permission on the `private` tag.
5. `tailscale serve --bg 443 http://127.0.0.1:8000`; Samba password: `sudo smbpasswd -a <user>` (owner types it).
6. Claude Code: install, then rerun `install.sh --system` (links it into `/usr/local/bin`), log in (owner).
   Rules and skills are already linked by step 3. Daily use: `ssh -t <host> tmux new -A -s assistant claude`.
7. `github-mirror` once by hand; check `~/github-mirror/last-run.txt`.
