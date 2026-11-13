# ARIS on Windows (through WSL 2)

> **Status: written from the documentation, and NOT yet run on a Windows computer.** Every step here was checked
> against Microsoft's WSL documentation, the usbipd-win project's documentation and source code, and ARIS's own
> code. Nobody has yet followed it on a real Windows machine with a real receiver. The helper script
> `deploy/windows/aris-usb.ps1` was checked by reading it and with a parser only; PowerShell itself was not
> available where it was written. [What was not verified](#what-was-not-verified) lists the open points. If a step
> does not match what you see, please open an issue and say which one.

ARIS is a Linux program. There is no Windows version, and none is planned. On Windows it runs inside **WSL 2**: a
small Linux computer that Windows runs for you, in the background. Two things then need a bridge:

- **The receiver.** It is plugged into Windows, but ARIS runs in Linux. A free program, **usbipd-win**, hands a USB
  device from Windows to WSL. A helper script that comes with ARIS does this for the receivers it knows.
- **The page.** ARIS's web page is served from inside WSL. Windows passes `http://localhost:5000` through to it, so
  any browser on the same computer opens it.

```
receiver --USB--> Windows --usbipd-win--> WSL 2 (Ubuntu): ARIS --> http://localhost:5000 in a browser on Windows
```

As everywhere, ARIS only receives and keeps metadata only. Nothing in this guide transmits.

## What you need

- **Windows 11, or Windows 10 version 2004 or newer** (build 19041 or higher). To see yours: press the Windows key
  and R, type `winver`, press Enter.
- A 64-bit computer (x64 or ARM64). ARIS and its radio tools have not been tried on ARM.
- **Virtualisation switched on** in the computer's firmware (BIOS / UEFI). Most computers have it on. If WSL stops
  with error `0x80370102`, it is off: the maker's manual says where the setting is.
- An account that may install programs (an administrator), for three things: installing WSL, installing usbipd-win,
  and sharing each receiver the first time.
- Free disk space: about 15 GB. The installer asks for about 10 GB for the AI part alone (Ollama and its model,
  about 4.7 GB); Ubuntu and the radio tools come on top. Without the AI part (`--no-ai`) a few GB are enough.
- Memory: WSL gets half of the computer's memory by default. With the AI chat inside WSL, 16 GB in the computer
  is comfortable. With 8 GB, leave the AI part out or see [The AI chat](#the-ai-chat-ollama).
- A receiver. An RTL-SDR dongle is the one this guide follows.

You do **not** need Zadig or any special Windows driver for the receiver: see
[Windows drivers](#windows-drivers-no-zadig).

## The steps

Commands marked **PowerShell** are typed in Windows. Commands marked **Ubuntu** are typed in the Ubuntu window
(the Linux side). To open PowerShell as an administrator: right-click the Start button and choose
"Terminal (Admin)" or "Windows PowerShell (Admin)".

### 1. Install WSL and Ubuntu

**PowerShell, as administrator:**

```powershell
wsl --install
```

Restart the computer when it asks. After the restart, open PowerShell again and type `wsl --install` once more:
current WSL only prepares Windows on the first run and fetches Ubuntu on the second. (On an older Windows 10 an
Ubuntu window may open by itself after the restart; then the second run is not needed.) Ubuntu starts and asks you
to choose a user name and a password. That is your Linux password: `sudo` asks for it later. From then on, open
Ubuntu from the Start menu.

- `wsl --install` installs Ubuntu. To pick a release yourself: `wsl --list --online` shows the names,
  and `wsl --install -d Ubuntu-24.04` installs that one. ARIS's installer needs Ubuntu or Debian.
- If `wsl --install` only prints help text, WSL is already there: use `wsl --install -d Ubuntu`.
- It must be WSL **2**, not WSL 1 (USB devices and systemd need a real Linux kernel). Check:

  ```powershell
  wsl --list --verbose
  ```

  The VERSION column must say 2. If it says 1: `wsl --set-version Ubuntu 2`.
- Bring WSL up to date. It needs Linux kernel 5.10.60.1 or newer for USB devices:

  ```powershell
  wsl --update
  ```

### 2. Check that systemd is on

systemd is the part of Linux that starts services and keeps them running. ARIS is installed as such a service.

**Ubuntu:**

```bash
systemctl is-system-running
```

Anything but `offline` means it is on: `running`, `starting`, or `degraded` (which only says that some unrelated
service failed). The Ubuntu that
`wsl --install` brings today has systemd on already, so most people can go to step 3.

If it prints `offline` or an error, switch it on (this needs WSL 0.67.6 or newer; `wsl --version` shows yours):

```bash
sudo nano /etc/wsl.conf
```

Add these two lines, save (Ctrl+O, Enter) and leave (Ctrl+X):

```ini
[boot]
systemd=true
```

Then, in **PowerShell**, stop WSL so that it starts fresh:

```powershell
wsl --shutdown
```

Open Ubuntu again from the Start menu and repeat the check.

### 3. Install usbipd-win

**PowerShell:**

```powershell
winget install --interactive --exact dorssel.usbipd-win
```

`--interactive` shows the installer's own windows. Without it, winget may restart the computer at once if the
drivers need that, so save your work first either way. No winget? Download the `.msi` from
<https://github.com/dorssel/usbipd-win/releases> and run it.

Then **close PowerShell and open a new window**: the old one does not know the new program yet.

Nothing has to be installed inside Ubuntu for this. Since usbipd-win 4.0.0 it brings its own Linux part along.
(Older guides tell you to install extra packages in Ubuntu first. That is no longer needed.)

### 4. Install ARIS inside Ubuntu

**Ubuntu:**

```bash
curl -fsSL https://raw.githubusercontent.com/bradderz143/ARIS-RF/main/deploy/get.sh | sudo bash
```

It first shows what it would do and asks `Install? [y/N]`. Nothing is installed before you answer `y`. (If Ubuntu
answers that `curl` is not found: `sudo apt update && sudo apt install -y curl`, then again.)

Or, step by step, from a copy of the code:

```bash
git clone https://github.com/bradderz143/ARIS-RF.git aris && cd aris
sudo bash deploy/install.sh --everything            # dry run: checks and shows the plan, changes nothing
sudo bash deploy/install.sh --everything --apply    # does it
```

The installer brings the radio tools, the Python modules, Ollama with the AI model, and ARIS itself as a service
that runs as its own user `aris`, never as root. Its own first lines say exactly what it does. If it reports that
systemd is not running, go back to step 2.

At the end it asks for the name and password of the web page's login. If it did not ask:

```bash
sudo -u aris env ARIS_DATA_DIR=/var/lib/aris python3 /opt/aris/aris_gui.py --set-password
```

### 5. Attach the receiver to WSL

Plug the receiver in. Leave the Ubuntu window open (WSL must be running).

First copy the helper from Ubuntu to Windows. **PowerShell:**

```powershell
wsl cat /opt/aris/deploy/windows/aris-usb.ps1 > "$HOME\aris-usb.ps1"
```

Look before you change anything. With no switch the helper only lists:

```powershell
powershell -ExecutionPolicy Bypass -File "$HOME\aris-usb.ps1"
```

(Windows does not run script files unless told to. `-ExecutionPolicy Bypass` allows it for this one run and changes
no setting.)

It prints the receivers it found, their state (`not shared`, `shared` or `attached`) and what it would do. It also
lists the other USB devices, which it leaves alone. The first lines of the script explain every switch.

Now attach. **The first time for each receiver, use PowerShell as administrator:**

```powershell
powershell -ExecutionPolicy Bypass -File "$HOME\aris-usb.ps1" -Attach
```

This does two things:

1. **Shares** the receiver (`usbipd bind`). Needs an administrator. Done once per receiver; Windows remembers it.
2. **Attaches** it to WSL (`usbipd attach --wsl`). Needs no administrator.

From then on an ordinary PowerShell window is enough for `-Attach`.

If your everyday Windows account is not an administrator, the administrator window belongs to another account.
There `$HOME` is that account's folder, so give the helper's full path instead
(`C:\Users\<your name>\aris-usb.ps1`). That account also has its own, empty WSL: the helper shares the receiver,
says that WSL is not running there, and stops. Then run `-Attach` once more from your own ordinary window.

**While a receiver is attached to WSL, Windows programs cannot use it.** To give it back:

```powershell
powershell -ExecutionPolicy Bypass -File "$HOME\aris-usb.ps1" -Detach
```

**Attaching does not last.** Do it again after:

- the receiver was unplugged and plugged back in,
- Windows restarted (and, as a rule, after the computer slept or hibernated),
- WSL stopped (`wsl --shutdown`, or by itself: see [Keeping it running](#keeping-it-running)).

Or let the helper do it:

```powershell
powershell -ExecutionPolicy Bypass -File "$HOME\aris-usb.ps1" -Auto
```

`-Auto` attaches the receivers and then stays running: usbipd attaches each one again when it comes back on the
same USB port. Leave that window open. Ctrl+C stops it. It ends when WSL stops, and it does not follow a receiver
to another USB port (start it again then).

A receiver the helper does not know (it knows RTL-SDR dongles, HackRF, Airspy, SDRplay RSP1 to RSPdx, LimeSDR-USB,
ADALM-Pluto, USRP B2xx and bladeRF by their USB IDs): take its bus id from the list and name it, for example
`-Attach -BusId 3-1`. The helper never touches a device that is neither in its list nor named by you.

The same by hand, if you would rather not run a script:

```powershell
usbipd list                          # find the receiver and its BUSID, for example 2-3
usbipd bind --busid 2-3              # share it: as administrator, once
usbipd attach --wsl --busid 2-3      # attach it: ordinary window, every time
usbipd detach --busid 2-3            # give it back to Windows
```

### 6. Check it inside Ubuntu

**Ubuntu:**

```bash
lsusb
```

An RTL-SDR dongle shows as `Realtek Semiconductor Corp. RTL2838 DVB-T` (or `RTL2832U`). If `lsusb` is missing:
`sudo apt install usbutils`.

### 7. Open the page

In any browser on the Windows computer: **<http://localhost:5000>**. Log in, then type in the chat:

- `/check` finds and tests the receivers and decoders, and changes nothing,
- `/setup` calibrates each receiver (a few minutes).

If `/check` finds no receiver although `lsusb` lists it, read the next section.

**A brand-new RTL-SDR dongle** carries the serial number `00000001`. ARIS lists such a dongle but does not use it
until it has a number of its own, and `/check` says so ([DEVICES.md](DEVICES.md) explains why). To give it one, in
**Ubuntu**, with only that dongle attached:

```bash
sudo systemctl stop aris
sudo rtl_eeprom -d 0 -s 00002002     # any 8 digits that no other dongle of yours has
sudo systemctl start aris
```

Then unplug the dongle, plug it back in, attach it again (step 5) and run `/check`.

## Access to the receiver inside WSL (udev and plugdev)

On Linux a service called **udev** decides who may use a USB device, at the moment the device appears. Ubuntu's
rtl-sdr package brings a rule (`60-librtlsdr*.rules`) that gives RTL-SDR dongles to the group `plugdev`, and the
ARIS service is a member of that group. That is why ARIS needs no root.

Under WSL this depends on two things:

- **systemd must be on** (step 2). udev is started by systemd. Without it nothing applies the rule, and the
  receiver belongs to root only.
- **The rule must be there before the receiver is attached.** In this guide ARIS is installed (step 4) before the
  receiver is attached (step 5), so the order is right. If you attached first: `-Detach`, then `-Attach`.

To check, find the receiver's bus and device number in `lsusb` (for example `Bus 001 Device 003`) and look at it.
**Ubuntu:**

```bash
ls -l /dev/bus/usb/001/003
```

- `crw-rw---- 1 root plugdev` is right.
- `crw-rw-r-- 1 root root` (or similar) means the rule was not applied. Then:

  ```bash
  systemctl is-active systemd-udevd      # should print: active
  sudo udevadm control --reload          # read the rules again
  ```

  and detach and attach the receiver again from Windows (`-Detach`, `-Attach`). If `udevadm` answers
  `Failed to send reload request: No such file or directory`, udev is not running: systemd is off (step 2).

Do not "fix" this by running ARIS as root or by opening the device to everyone.

On an ordinary Linux computer the kernel's television driver can take an RTL-SDR dongle away from the radio tools,
and ARIS's installer writes a file that prevents it. The Linux kernel that WSL brings (its 6.6 and 6.18 configurations
were read for this guide) is built without that driver, so under WSL this should not come up.

### Windows drivers: no Zadig

Windows programs for RTL-SDR dongles need a driver swapped in with a tool called Zadig. This route does not: while
a receiver is attached, usbipd-win holds it with its own driver, whatever driver Windows had on it, and the Linux
side uses Linux's own. A dongle that never saw Zadig is fine, and so is one that did.

## Opening the page from another device

By default only the Windows computer itself can open the page: WSL sits behind an internal network of its own, and
Windows passes on only its own `localhost`.

> **ARIS speaks plain HTTP.** The password and everything on the page cross the network unencrypted. Open it to
> other devices only on a network you trust (your own home network), never on a public or shared one.

There are two ways, by Windows version. Both are typed in **PowerShell as administrator**.

If your everyday Windows account is not an administrator (see step 5): edit `.wslconfig` and run the
`wsl hostname -I` line in your own ordinary window, because both belong to your account. Only the
`New-NetFirewall...` and `netsh` lines need the administrator window; type the address in there by hand in place of
`$wslAddress`.

**Windows 11 (22H2 or newer): mirrored networking.** WSL then shares the computer's own network addresses.
Create or edit the file `.wslconfig` in your user folder (`notepad "$HOME\.wslconfig"`) so that it has:

```ini
[wsl2]
networkingMode=mirrored
```

Restart WSL (`wsl --shutdown`, open Ubuntu again, attach the receiver again), then allow the page through the
firewall that guards WSL:

```powershell
New-NetFirewallHyperVRule -Name "ARIS" -DisplayName "ARIS web page" -Direction Inbound -VMCreatorId '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' -Protocol TCP -LocalPorts 5000
```

Other devices then open `http://<this computer's address>:5000` (`ipconfig` shows the address). To undo:
`Remove-NetFirewallHyperVRule -Name "ARIS"`, and take the line out of `.wslconfig`.

**Windows 10, or Windows 11 without mirrored networking: a port proxy.** Windows listens on port 5000 and passes
connections to WSL's internal address:

```powershell
$wslAddress = (wsl hostname -I).Trim().Split(' ')[0]
netsh interface portproxy add v4tov4 listenport=5000 listenaddress=0.0.0.0 connectport=5000 connectaddress=$wslAddress
New-NetFirewallRule -DisplayName "ARIS web page" -Direction Inbound -Protocol TCP -LocalPort 5000 -Action Allow -Profile Private
```

`-Profile Private` lets it work only while Windows treats the network as private (a home network). WSL's internal
address changes when WSL restarts; then delete the proxy and add it again:

```powershell
netsh interface portproxy delete v4tov4 listenport=5000 listenaddress=0.0.0.0
```

To undo for good: that line, and `Remove-NetFirewallRule -DisplayName "ARIS web page"`.

## The AI chat (Ollama)

The chat answers common requests by itself. Open questions go to an AI model that runs on your own computer
through a program called Ollama. There are two places it can run.

**Inside WSL (what the installer sets up).** The installer runs Ollama's own installer inside Ubuntu, which adds
Ollama as a second service, and downloads the model (about 4.7 GB). Nothing else to do. Two things to know:

- *Memory.* WSL may use half of the computer's memory unless told otherwise. To change that, put for example
  `memory=12GB` under `[wsl2]` in `.wslconfig` (see above for the file) and run `wsl --shutdown`.
- *Graphics card.* Inside WSL, Ollama can use an **NVIDIA** card and no other kind. Install the current NVIDIA
  driver on Windows (Windows 11, or Windows 10 21H2 or newer); WSL passes the card through. `nvidia-smi` in Ubuntu
  should then list it. Without a usable card the model runs on the processor: it works, slowly.

**On Windows itself (Ollama for Windows).** Useful with an AMD card, or if you already use Ollama on Windows.
Install ARIS without its AI part: add `--no-ai` in step 4 (the one-line command then ends in
`| sudo bash -s -- --no-ai`; from a copy of the code it is `sudo bash deploy/install.sh --everything --no-ai --apply`).
Install Ollama for Windows and fetch the model there (`ollama pull qwen2.5:7b`). ARIS finds Ollama at the address in
the environment variable **`ARIS_OLLAMA_HOST`** (default `http://127.0.0.1:11434`). What that needs depends on the
networking mode:

- *Mirrored networking (Windows 11, see above):* nothing. Linux reaches Windows programs at `127.0.0.1`, which is
  already ARIS's default, and Ollama stays closed to the rest of the network. Do not also run Ollama inside WSL:
  both would want the same port.
- *The default (NAT) networking:* more work, and less private. Ollama for Windows listens on `127.0.0.1` only, which
  WSL cannot reach. You would have to make it listen on all addresses (set the Windows environment variable
  `OLLAMA_HOST` to `0.0.0.0`, then quit and start Ollama), allow port 11434 through Windows Firewall for WSL, find
  the address of Windows as WSL sees it (**Ubuntu:** `ip route show | grep -i default | awk '{ print $3}'`), and
  give it to the ARIS service:

  ```bash
  sudo mkdir -p /etc/systemd/system/aris.service.d
  printf '[Service]\nEnvironment=ARIS_OLLAMA_HOST=http://ADDRESS:11434\n' | sudo tee /etc/systemd/system/aris.service.d/ollama.conf
  sudo systemctl daemon-reload && sudo systemctl restart aris
  ```

  (`ADDRESS` is the address you just found.) Ollama has no password, so anything that can reach port 11434 can use
  it: keep the firewall rule as narrow as you can. In this mode, Ollama inside WSL is the simpler and safer choice.

## Keeping it running

On a Linux computer ARIS starts with the computer and runs day and night. **On Windows it does not, unless you
arrange it**, for three reasons:

1. **WSL stops a Linux system that nothing on the Windows side is using.** A few seconds after the last Ubuntu
   window (or `wsl` command) closes (15 seconds by default in current WSL), WSL stops Ubuntu, and ARIS with it.
   Services running inside, such as ARIS, do not count as "in use". About a minute later the whole WSL machine
   stops.
2. **WSL does not start by itself.** After Windows starts, nothing runs in WSL until something opens it.
3. **The receivers are not attached by themselves** after a restart of Windows or of WSL.

The plain way: **leave an Ubuntu window open** (minimised is fine), and leave a PowerShell window running the
helper with `-Auto`. As long as both are open and the computer does not sleep, the station runs.

Without the open Ubuntu window (WSL 2.5.4 or newer; `wsl --version`): tell WSL never to stop an idle Linux system.
In `.wslconfig` in your user folder:

```ini
[general]
instanceIdleTimeout=-1
```

Then `wsl --shutdown` once. From then on Ubuntu keeps running once it has been started, until `wsl --shutdown` or
until Windows restarts. After each restart of Windows you still have to start it (open Ubuntu once, or type
`wsl true` in PowerShell) and attach the receivers again. (`vmIdleTimeout` under `[wsl2]`, Windows 11 only, is the
separate timer for the whole WSL machine; 60 seconds by default, a negative number switches it off.)

Also switch off sleep in Windows' power settings while the station should run: a sleeping computer receives
nothing, and the receivers have to be attached again afterwards.

For a station that should run unattended, day and night, use a computer that runs Linux itself.

## What does not work here, or is untested

- **Everything in this guide** is untested on real hardware: see the status at the top.
- **The passive Wi-Fi view** (`deploy/aris-wifi.sh` and its helper): **untested, and it depends on the Linux
  kernel of your WSL.** It needs a USB Wi-Fi adapter (attach it with `-BusId`) and that adapter's Linux driver. WSL
  kernels of the 6.6 line are built with almost no USB Wi-Fi drivers; the 6.18 line has the common ones (`uname -r`
  in Ubuntu shows yours, `wsl --update` brings the current one). The adapter may also need its firmware files, which
  WSL's Ubuntu does not bring (`sudo apt install linux-firmware`). Nobody has tried it. (The Wi-Fi card built into
  a computer is normally not a USB device, so it cannot be handed to WSL at all.)
- **The Bluetooth view** (`deploy/aris-bt.sh`, an Ubertooth One): **untested.** In principle the Ubertooth is one
  more USB device (attach it with `-BusId`); nobody has tried it under WSL.
- **High sample rates.** Every sample travels from Windows to WSL through usbipd's network link. Whether an RTL-SDR
  keeps up at 2.4 million samples a second (what band captures use) without losing samples has not been measured.
  To measure it yourself, in **Ubuntu**:

  ```bash
  sudo systemctl stop aris
  rtl_test -s 2400000        # let it run for a minute, then Ctrl+C; "lost at least ..." lines mean lost samples
  sudo systemctl start aris
  ```

  If it loses samples: plug the receiver straight into the computer, not into a hub, and close other programs
  that keep the computer busy.
- **SoapySDR receivers** (HackRF, Airspy, SDRplay, LimeSDR, PlutoSDR, USRP, bladeRF): ARIS has run with these in
  simulation only, on any system. The helper attaches them; whether they then work under WSL is untested. Some
  restart themselves while they load their firmware, which detaches them: use `-Auto`.
- **Signal Hound receivers** (BB60C / BB60D, SA44B / SA124B): untested. Their plug-ins need Signal Hound's Linux
  libraries inside Ubuntu, and a BB60 moves far more data than an RTL-SDR. Attach one with `-BusId`.
- **Running day and night**: only with the arrangements in [Keeping it running](#keeping-it-running).
- **HTTPS through Tailscale** (`deploy/tailscale-https.sh`): not tried under WSL.

## Troubleshooting

Messages that start with `usbipd:` are usbipd's own; the helper shows them and adds what to do.

| What you see | What to do |
|---|---|
| PowerShell says that running scripts is disabled on this system | Start the helper as shown: `powershell -ExecutionPolicy Bypass -File ...` |
| `usbipd` is not recognised | Open a new PowerShell window after installing it. Still missing: step 3 |
| `Device is not shared; run 'usbipd bind --busid ...' as administrator first` | Run `-Attach` once in PowerShell opened as administrator |
| `Access denied; this operation requires administrator privileges` | The same: sharing (`bind`) needs an administrator |
| `There is no WSL 2 distribution running` | Open Ubuntu from the Start menu and leave the window open |
| `WSL kernel is not USBIP capable; update with 'wsl --update'` | The Linux kernel of WSL is too old: `wsl --update`, then `wsl --shutdown`, open Ubuntu again |
| `A firewall appears to be blocking the connection; ensure TCP port 3240 is allowed` | usbipd-win adds a rule named `usbipd` to Windows Firewall. Another firewall product, or a company policy, needs TCP port 3240 allowed from WSL to this computer |
| `Device busy`, or `The device appears to be used by Windows` | A Windows program has the receiver open: close it (any SDR program). If none has: as administrator, `usbipd bind --force --busid <id>` (Windows then cannot use the receiver until `usbipd unbind`; a restart may be needed) |
| `Device with busid '...' is already attached to a client` | It is attached already. Nothing to do |
| `lsusb` lists the receiver, `/check` finds none | Access rights: [udev and plugdev](#access-to-the-receiver-inside-wsl-udev-and-plugdev). Then **Ubuntu:** `sudo journalctl -u aris -n 50` |
| The receiver is gone after sleep, a replug or a restart | Attach it again (`-Attach`), or use `-Auto` |
| `http://localhost:5000` does not open | Is an Ubuntu window open? **Ubuntu:** `systemctl status aris`. Not active: `sudo journalctl -u aris -n 50` |
| ARIS does not start, and its log says the address is already in use | Another program has port 5000 (with mirrored networking that can be a Windows program). Give ARIS another port: a file like the `ollama.conf` above with `Environment=ARIS_GUI_PORT=5080`, then open `http://localhost:5080` |
| The AI does not answer | **Ubuntu:** `systemctl status ollama`. Not enough memory is the usual cause: see [The AI chat](#the-ai-chat-ollama) |

## Removing it

- Give the receivers back and stop sharing them (PowerShell as administrator):
  `powershell -ExecutionPolicy Bypass -File "$HOME\aris-usb.ps1" -Detach -Unshare`
- Remove the ARIS service (**Ubuntu**): `sudo bash /opt/aris/deploy/install.sh --remove`. It keeps the code and
  your data and says where they are.
- Remove usbipd-win: `winget uninstall usbipd`.
- Remove Ubuntu with **everything in it, ARIS's data included**: `wsl --unregister Ubuntu`.
- Take out what you added to `.wslconfig`, and the firewall rules and port proxy if you made them (their sections
  say how).

## What was not verified

Nothing here was run on Windows. On top of that, these points rest on something weaker than a document that was
read for this guide:

- That Ubuntu under WSL starts udev like an ordinary Ubuntu, so that the rtl-sdr rule is applied when the receiver
  is attached. The usbipd-win project's notes say udev rules work and must be in place before attaching; the check
  in [the udev section](#access-to-the-receiver-inside-wsl-udev-and-plugdev) tells you.
- What `wsl --install` does after the restart on each Windows version (read from WSL's source code for current
  WSL; not tried).
- How well an RTL-SDR holds 2.4 million samples a second through usbipd.
- That no Windows driver matters (no Zadig): it follows from how usbipd-win works, and was not tried.
- The NVIDIA part: NVIDIA's own guide for WSL could not be reached while this was written. Microsoft's page (driver
  on Windows) and Ollama's installer (NVIDIA only under WSL) were read.
- Ollama for Windows reached from WSL in the default (NAT) networking: the pieces are documented one by one (how
  Ollama listens, how WSL finds Windows' address); the whole was not tried, and the firewall rule is left to you.
- The port proxy together with Windows' own `localhost` pass-through on the same port.
- What Windows does to WSL and to an attached receiver when the computer sleeps, and when you sign out.
- The case of an administrator window that belongs to another Windows account (WSL keeps its Linux systems per
  account; the helper's behaviour there follows from that and was not tried).
- Windows on ARM.
- The helper script on any PowerShell: it was parsed (no syntax error found) and read line by line against the
  rules of Windows PowerShell 5.1, not run.

## Sources

- Microsoft, Install WSL: <https://learn.microsoft.com/windows/wsl/install>
- Microsoft, Connect USB devices: <https://learn.microsoft.com/windows/wsl/connect-usb>
- Microsoft, Use systemd with WSL: <https://learn.microsoft.com/windows/wsl/systemd>
- Microsoft, Advanced settings (`wsl.conf`, `.wslconfig`): <https://learn.microsoft.com/windows/wsl/wsl-config>
- Microsoft, Networking with WSL (NAT, mirrored, port proxy): <https://learn.microsoft.com/windows/wsl/networking>
- Microsoft, GPU acceleration in WSL: <https://learn.microsoft.com/windows/wsl/tutorials/gpu-compute>
- Microsoft, WSL's source code (when a Linux system counts as idle; the Linux kernel's configuration):
  <https://github.com/microsoft/WSL>, <https://github.com/microsoft/WSL2-Linux-Kernel>
- usbipd-win, README, the "WSL support" page of its wiki, and its source (messages, what needs an administrator,
  the output of `usbipd state`): <https://github.com/dorssel/usbipd-win>
- Ollama, its FAQ, Windows and Linux pages and its Linux install script (read, not run):
  <https://github.com/ollama/ollama/tree/main/docs>
- The USB IDs in the helper: each project's own udev rules (rtl-sdr, HackRF, Airspy, LimeSuite, PlutoSDR, UHD,
  bladeRF) and the USB ID list of linux-usb.org; the helper's comments say which is which.
