# Windows build node (build-host.example.com)

Windows is **not** an ephemeral overlay like the Linux/Android profiles: it is a
long-lived VM (`macp-win-build`) that MACP drives over SSH through cmd.exe using
the `windows-ltsc` profile.

## What is installed

| Item | Value |
|---|---|
| Image | Windows 10 Enterprise LTSC 2021 (zh-CN), from `/mnt/nfs/system-images/SW_DVD9_WIN_ENT_LTSC_2021_64BIT_ChnSimp_MLF_X22-84402.ISO` |
| Hardware | 4 vCPU, 6 GB RAM, 40 GB SATA disk (BIOS/MBR — no virtio drivers needed) |
| Access | OpenSSH Server, `Administrator` + the KVM node key, autostart enabled |
| Toolchain | in-box .NET Framework `csc.exe` (`C:\Windows\Microsoft.NET\Framework64\v4.0.30319`) |
| Workspace | `C:\macp\work` (recreated per job by the node) |

## Build it

```bash
cd /opt/macp-kvm-node/windows
./build-windows-vm.sh                 # unattended install (~20 min)
./build-windows-vm.sh --status        # domain, lease, disk
./build-windows-vm.sh --screenshot /tmp/win.png
```

Notes from actually running it:

* The ISO waits for "press any key"; the script pokes `KEY_ENTER` through
  `virsh send-key` until the disk starts growing, otherwise a headless install
  falls through to an empty disk.
* The answer file (`autounattend.xml`) needs `chmod 711 /opt/macp-win` so the
  `libvirt-qemu` user can read the generated ISO — otherwise QEMU refuses to
  start with "Permission denied".
* `Add-WindowsCapability OpenSSH.Server` installs the service but does not start
  it, and the `FirstLogonCommands` that should start it do not always run. If
  port 22 stays closed, see "seed" below.
* Opening the Run dialog and typing a drive path through `virsh send-key` is
  unreliable (shift chords are pressed sequentially, so `E:\` becomes `;\`).
  Password SSH + `powershell -Command -` over stdin is the dependable route.

## Seed / repair over password SSH

`/opt/macp-win/admin_password` holds the Administrator password (0600, never
printed). With it:

```bash
PW="$(cat /opt/macp-win/admin_password)"
sshpass -p "$PW" ssh -o PreferredAuthentications=password -o PubkeyAuthentication=no \
  Administrator@$(virsh -c qemu:///system net-dhcp-leases default | awk '/macp-win-build/{print $5}' | cut -d/ -f1) \
  "powershell -NoProfile -Command -" < seed/setup.cmd.ps1
```

`seed/setup.cmd.ps1` sets `sshd` to automatic, installs the node key into
`C:\ProgramData\ssh\administrators_authorized_keys` with the ACL that sshd
requires (Administrators + SYSTEM only), creates `C:\macp\work`, writes
`C:\macp-node-ready.txt` and restarts sshd.

## Use it from MACP

```json
{
  "execution": {"profile": "windows-ltsc", "collect": ["dist/**"]},
  "acceptance": [{"gate": "windows-build", "required": true}]
}
```

The gate runs the fixed, server-side allowlisted command `cmd /c build.bat`; the
task supplies `build.bat` and its sources. Outputs under `dist/` come back with
SHA-256 and an `artifact.produced` event. Verified: a 4-second gate produced
`dist/hello.exe` (3,584 B) via the in-box `csc`.

## Not done yet

* Visual Studio Build Tools / MSVC (C++), Windows SDK — needs the Microsoft
  installer payload (~2 GB) inside the guest.
* Activation (installs unactivated with the public KMS client key).
* `windows-test` gate and a Windows artifact cache.
