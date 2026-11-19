# Early NIC identity validation

## Scope

Real-guest validation uses Enterprise SONiC 4.6, two dataplane NICs, the default
`tc` datapath, two vCPUs, and 4096 MiB RAM. Virtio, PCI assignments, MAC assignments,
HwSKU mappings, and logical port names are retained. The source change includes
no dataplane emulation fallback and no weakening of routing readiness checks.

The precise interleaving behind the intermittent natural cold-boot reversal is
not established. Twelve unmodified baseline cold starts passed. These passes
are not evidence of a fix or a statistical comparison. Instead, the identity
invariant is exercised with a deterministic early-boot registration reversal.

## Boot-time reversal: baseline red, fixed green

The opt-in disposable-guest `nic_boot_fault.py` service runs after enumeration
but before identity assignment, networking, and SONiC. It unbinds only the two
inactive dataplane virtio devices, then rebinds them in reverse order. Management
is retained. Before/after records include physical MACs, PCI paths, flags, and
boot identity. This is explicit fault injection, not a naturally occurring race.

- **Original launcher:** injection at 2.63 seconds produced both wrong `ethN`
  identities with unchanged PCI/MAC wiring. The mapping checker rejected both
  interfaces and IPv4 session readiness failed before any maintenance operation.
- **Identity service:** the same reversal happened at 2.81 seconds. Identity
  assignment finished at 3.00 seconds, before `sonic.target` at 9.81 seconds,
  SWSS at 13.08 seconds, networking at 16.35 seconds, and syncd at 19.40 seconds.
  Both interfaces then matched launcher intent. All four IPv4/IPv6 BGP sessions
  and route propagation passed. The full dry-run, drain, status, idempotency, and
  exact BGP configuration restoration workflow passed on that boot.

This establishes correction before consumers activate, not a live wiring repair
after a readiness failure. The injected unit is never included in the Docker
image, and is absent on clean acceptance boots.

## Ambiguous identity: fail closed

An explicit disposable-guest environment override makes both dataplane devices
have the same MAC after reverse registration. Firmware intent remains unchanged.
Serial evidence from the failed boot proves:

- The identity service entered `failed` and wrote no completion marker.
- Networking, configuration setup, SWSS, syncd, and `sonic.target` remained
  inactive, with both activation timestamps zero: none started and then stopped.
- The guest did not become reachable over SSH; consumer gating is independently
  established by the serial unit-state evidence, not inferred from that symptom.

The serial proof listener starts before assignment and survives emergency-target
isolation. Initial collector attempts failed; those attempts were not counted as
passes. The successful collector uses the launcher's Scrapli virtual environment.

## Clean-start acceptance

Six fresh-overlay cycles on the standard-built image are in progress. Each must
verify current firmware intent against physical MACs, current-boot marker and
helper digest, retained virtio driver, unit ordering, no injected test files,
inactive ZTP with discovery mode absent, global IPv6 addresses, and the full
exact-restoration workflow. Do not treat this section as completed acceptance
until those cycles finish.

## Offline coverage and limits

- 78 driver tests pass, including every three-NIC registration permutation,
  management participation in cycles, sparse NIC MAC assignment, exact emitted
  QEMU intent, fresh intent on repeated launches, firmware changes, current-boot
  marker checks, rejection before mutation, and restoration gated on setup reboot.
- Helper/test Ruff and whitespace checks pass. Launcher Ruff findings match the
  unchanged base exactly: UP008, two UP031, and F821 in the existing login path.
- The validated ZTP cleanup and configuration restore functions are unchanged.
- Other SONiC versions, the `macvtap` datapath, VM snapshot restoration, and
  dataplane throughput have not been exercised by this validation. Unit checks
  of emitted MAC preservation do not substitute for those live tests.
