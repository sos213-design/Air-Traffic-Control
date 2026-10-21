# Air Traffic Control Simulator

A multiplayer air traffic control game. One computer runs the server and the
control tower radar; players join as pilots from their phones by scanning a
QR code.

| Page | Address | Who uses it |
|---|---|---|
| Start page | `http://localhost:8000` | The host: shows the QR code for pilots |
| Control tower | `http://localhost:8000/tower` | The radar, on as many screens as you like |
| Cockpit | `http://<computer's address>:8000/cockpit` | Pilots, on their phones |

## Running it

You need Python 3.10 or newer. On Windows, install it from
[python.org](https://www.python.org/downloads/) and tick **"Add python.exe to
PATH"** during setup.

```
cd Air-Traffic-Server
python -m venv .venv
.venv\Scripts\activate          (Windows)
source .venv/bin/activate       (Mac / Linux)
pip install -r requirements.txt
python AirTrafficMain.py
```

The `venv` and `pip install` steps are only needed the first time. After that,
activate the environment and run `python AirTrafficMain.py`.

Keep that terminal open while you play. It prints the addresses to use and a
QR code. Press Ctrl+C to stop the server.

Always start it with `python AirTrafficMain.py`. Starting it with a plain
`uvicorn AirTrafficMain:app` only lets this computer connect, not phones. If
you prefer `uvicorn`, add `--host 0.0.0.0`.

## Joining from a phone

1. Connect the phone to the **same Wi-Fi network** as the computer. Turn off
   mobile data so the phone doesn't fall back to it.
2. On the computer, open `http://localhost:8000`. Don't double-click
   `Open_Page.html`; it has to come from the server.
3. Scan the QR code with the phone's camera and tap the link it shows. Or type
   the address under the QR code into the phone's browser, starting with
   `http://`.
4. In the cockpit, the badge at the top right should say **CONNECTED**. Enter
   a callsign and press **Register Flight**. Your plane appears on the tower
   radar.

## If the phone can't connect

Work through these in order. After each change, reload the page on the phone.

**The start page shows a red warning box.** Do what it says; it means the
server was started in a way phones can't reach.

**The phone says "can't reach this page" or keeps loading.**

- **Windows firewall.** The first time you run the server, Windows asks
  whether to allow Python on the network. If you missed it or clicked Cancel:
  Start → search **"Allow an app through Windows Firewall"** → **Change
  settings** → find every **Python** entry → tick both **Private** and
  **Public** → OK. Then restart the server.
- **Network set to Public (Windows).** Settings → Network & internet → Wi-Fi →
  your network → set **Network profile type** to **Private**.
- **Mac firewall.** If macOS asks whether Python may accept incoming
  connections, click **Allow**. To check later: System Settings → Network →
  Firewall → Options.
- **Wrong network address.** The computer may have several (VPNs, VirtualBox,
  Docker, Hyper-V). Open **"Phone can't connect?"** on the start page. If it
  lists more than one address, tap each one in turn and try the new QR code.
  You can also find the Wi-Fi address yourself: run `ipconfig` (Windows) and
  look for the **IPv4 Address** under your Wi-Fi adapter, usually starting
  with `192.168.` or `10.`. Then use `http://THAT-ADDRESS:8000/cockpit`.
- **VPN.** Turn off any VPN on both the computer and the phone.
- **Guest or school/work Wi-Fi.** Many guest, hotel, school and office networks
  stop devices from talking to each other. Use a home network, or turn on
  your phone's hotspot and connect the computer to it.
- **WSL.** If you run the server inside WSL on Windows, phones usually can't
  reach it. Run it with normal Windows Python instead.

**The cockpit opens but says OFFLINE.** The server stopped, or the phone lost
Wi-Fi. Check the terminal on the computer is still running, then reload.

**The phone's browser changes the address to `https://`.** Type the address
again with `http://` at the start. In Chrome on Android, if it insists, turn
off Settings → Privacy and security → **Always use secure connections**.

**Quick test from the computer.** Open the phone address (for example
`http://192.168.1.23:8000/cockpit`) in the computer's own browser. If that
fails too, the server isn't reachable on the network: check the warning on the
start page. If it works on the computer but not the phone, it's the firewall
or the Wi-Fi network.
