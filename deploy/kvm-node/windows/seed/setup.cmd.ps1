# Runs inside the Windows build node (piped to `powershell -Command -`).
#
# Sets sshd to start automatically, installs the KVM node public key where sshd
# expects it for an Administrator account, and prepares the workspace root.
# __NODE_KEY__ is replaced by the caller (see README.md) so the key never lives
# in git.

$ErrorActionPreference = 'Stop'
$key = '__NODE_KEY__'

New-Item -Force -Path 'C:\ProgramData\ssh' -ItemType Directory | Out-Null
$path = 'C:\ProgramData\ssh\administrators_authorized_keys'
Set-Content -Path $path -Value $key -Encoding ascii
# sshd rejects the file unless only Administrators and SYSTEM can touch it.
icacls $path /inheritance:r /grant 'Administrators:F' /grant 'SYSTEM:F' | Out-Null

sc.exe config sshd start= auto | Out-Null
New-Item -Force -Path 'C:\macp\work' -ItemType Directory | Out-Null
'ready' | Out-File 'C:\macp-node-ready.txt' -Encoding ascii
Restart-Service sshd
'CONFIGURED ' + (Get-Service sshd).Status
