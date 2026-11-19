# Dell Enterprise SONiC

This is the vrnetlab docker image for SONiC's VM.
The scripts in this directory are based on FreeBSD and VSRX kinds.

> Available with [containerlab](https://containerlab.dev) as [`dell_sonic`](https://containerlab.dev/manual/kinds/dell_sonic/) kind.

## Building the docker image

Download the latest Dell SONiC qcow2 disk image from Dell support website. Rename the file to `dell-sonic-[version].qcow2` and run `make`.

After typing `make`, a new image will appear named `vrnetlab/vr-dell_sonic:<version>`. Run `docker images` to confirm this.

## System requirements

- CPU: 2 cores
- RAM: 4GB
- DISK: ~3.2GB

## Configuration

SONiC nodes boot with a basic configuration by default, enabling SSH and basic management connectivity. All factory default configuration is retained.
Full startup configuration can be passed by mounting it under `/config/config_db.json`, this is done automatically by Containerlab. Only SONiC json config format is accepted. This fill will replace existing default config.

The startup config is copied into the VM over SSH. Before that, if the guest has an executable `/etc/network/if-up.d/ntpsec-ntpdate` hook (SONiC 4.6), the launcher removes its execute bit and stops the `systemctl start ntpsec.service` call the boot is waiting on. That hook deadlocks the 4.6 boot: `networking.service` waits for the hook, the hook waits for `ntpsec.service`, and `ntpsec.service` is ordered after units that wait for `networking.service`. The first boot then never starts `hostcfgd` or reaches `multi-user.target`, and every later boot hangs before the login prompt. The change persists on the guest disk; NTP itself still runs. Images without the hook are not changed. Then the launcher brings up the management interface over the serial console (address, link, default route). It then waits until `eth0` is up, SSH listens on IPv4, and `config-setup` has finished with `CONFIG_DB_INITIALIZED` set, so `config load` does not race the boot-time CONFIG_DB reload, and until the `bgp` container reports up in STATE_DB (`FEATURE|bgp`). Its config daemon renders the FRR configuration from CONFIG_DB when the container starts and follows changes only once it is initialized, so routing settings loaded in between would not reach FRR. These steps are idempotent and do not write to CONFIG_DB, so management settings in a supplied startup config are kept. After this wait, each serial-console prompt wait is limited to 120 seconds. If the shell prompt does not appear in time, the launcher logs an error and exits instead of hanging.

When a startup configuration is supplied, the launcher first quiesces first-boot
ZTP discovery over the root serial console. It stops `ztp.service`, checks that
it is inactive, removes `ZTP|mode` from CONFIG_DB, and synchronously restarts
`interfaces-config` to stop the discovery DHCP workers, following SONiC's own
`stop_ztp_dhcp` sequence. Otherwise a late DHCPv6 `PREINIT6` can flush global IPv6
addresses just restored by intfmgrd while leaving CONFIG_DB unchanged. Cleanup
has a 180-second timeout (with a five-second forced-kill grace period); command
failure or a missing completion marker aborts startup before configuration
restore. An absent ZTP unit is allowed. No factory fallback config is loaded and
no saved config is created by cleanup. Management settings are reapplied after
cleanup. Without a supplied startup configuration, ZTP is left alone. The SSH
restore subprocess is bounded to 600 seconds and its command arguments are not
included in failure diagnostics.

### NIC identity

The launcher retains `virtio-net-pci`. Kernel allocation-order `ethN` names are
not treated as physical interface identity: the same PCI/MAC wiring can receive
different names when network devices register in a different order. The guest's
HwSKU lanemap, however, requires those names to refer to the intended ports.

The launcher records the exact MACs emitted to QEMU for management and dataplane,
including snapshot MACs and explicit MACs for sparse-topology gap NICs. It passes
that map through QEMU's read-only `opt/vrnetlab/nic-identity` firmware entry. The
guest requires the `qemu_fw_cfg` kernel driver; unsupported or mismatching firmware
intent aborts provisioning before startup configuration restore. Intent is read
afresh each boot, not saved as a potentially stale MAC map on the guest disk.

On a new guest overlay, the launcher installs an early identity service over the
serial console and reboots once **before** restoring the supplied configuration.
Installation never renames live interfaces. On the next boot the service waits
for udev enumeration, resolves physical interfaces by MAC, and validates the whole
map before changing anything. It frees conflicting names using temporary names,
then assigns the intended `ethN` names. Missing or duplicate MACs, unrelated name
occupants, temporary-name conflicts, and active interfaces requiring renaming
fail closed. The service is required before `sysinit.target`, networking, and
`sonic.target`; a failure blocks their startup rather than allowing SONiC to bind
to the wrong physical interfaces. HwSKU files and logical port names are unchanged.

The launcher requires a current-boot completion marker tied to the helper code
and exact firmware intent, an active identity service, and a fresh mapping check
before it proceeds with bootstrap and startup restore. One automatic setup reboot
is allowed per launcher invocation; a missing marker afterward aborts startup.
The installed service runs on subsequent guest boots too. A new overlay or helper
version needs the setup reboot again.

See [NIC validation](tests/NIC-VALIDATION.md) for fault-injection evidence and
the tested scope. The fault injector is not included in production images.

### Hostname

The hostname is chosen as follows:

1. If the startup config sets `DEVICE_METADATA.localhost.hostname`, that hostname is used. It is never replaced by the node name.
2. Otherwise the Containerlab node name (`--hostname`) is used. A startup config without a hostname keeps it, because `config load` merges `DEVICE_METADATA` fields.

If the chosen name is not a valid SONiC hostname, or the startup config cannot be read, the launcher logs a warning. An invalid hostname is not applied. An unreadable startup config falls back to the node name, and loading it then fails as usual.

After the readiness wait, which also waits for the boot-time `hostname-config` unit to finish, the launcher writes the hostname to CONFIG_DB (`DEVICE_METADATA|localhost`). If `hostcfgd` is running (older images), the launcher waits up to 20 seconds for it to apply the change. `hostcfgd` ignores a change that arrives while it is still starting, so if the live hostname does not match by then, or `hostcfgd` is not running, the launcher applies it to `/etc/hostname`, the kernel hostname, and a `127.0.0.1 <hostname>` entry in `/etc/hosts`. These are the same steps SONiC's own `hostname-config` and `hostcfgd` use, and running after them avoids a duplicate `/etc/hosts` entry. Other `/etc/hosts` entries are kept, and repeated runs do not add duplicate entries. If `/etc/sonic/config_db.json` already exists (older images), its hostname is updated too. The file is not created when it is absent, because creating it would change the next boot (for example, disable ZTP); a supplied startup config is saved to it by the loader. Without a startup config, the hostname therefore reverts to the factory default after a guest reboot.

This is needed because on the SONiC 4.6 VM `hostname-config` runs before the startup config is loaded and `hostcfgd` may not have started yet, so loading a new hostname into CONFIG_DB does not reliably change the live hostname. Setting only the live hostname (for example with `hostnamectl`) is not enough either, because SONiC re-applies the CONFIG_DB hostname.

## Known limitations

- The launcher bootstraps the guest once. It does not re-run the bootstrap after an in-guest reboot, so settings applied at startup but not saved in the guest (the hostname without a startup config, a static management address that is not in the startup config) are not reapplied.
- Restarting the container (`docker restart`) reuses the existing disk overlay, so the guest keeps its disk state. The launcher logs in with the factory password and, after `Login incorrect`, with the configured password (sending an empty line if the guest shows no new login prompt within 10 seconds), then runs the bootstrap and startup config load again. Under Containerlab, a restarted container does not get its data links back; recreate the node.

## Running tests

```
python3 -m unittest discover -s dell/dell_sonic/tests
```
