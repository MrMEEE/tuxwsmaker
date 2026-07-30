# TODO

- Fix qcow2 and ISO download/detection; current Red Hat catalog discovery is still unstable and needs a proper follow-up.
- Keep PXE responsibilities separate:
	- Builder VM PXE preparation (inside builder workflow)
	- Host machine PXE rollout services for completed image deployment
- Runtime dependency reminder:
	- Install guestfs-tools on the host (provides virt-customize used by Builder VM SSH key injection/provisioning)
	- Install python3-gi on the host (provides gi module required by virt-install)
	- Install virtiofsd on the host (required for virtiofs-backed Builder ISO share mount)
