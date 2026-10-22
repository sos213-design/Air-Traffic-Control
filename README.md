# Air Traffic Control Simulator

A multiplayer air traffic control game played in the web browser. One server
runs the airspace; players join from their own computers as pilots, and
anyone can watch the control tower radar.

| Page | Address | Who uses it |
|---|---|---|
| Start page | `/` | The host: shows the links to send out |
| Control tower | `/tower` | The radar, on as many screens as you like |
| Cockpit | `/cockpit` | Pilots |

## Share a link with anyone

To send pilots a link that works from anywhere, put the server online. The
steps below use [Render](https://render.com), which has a free plan and
deploys straight from this GitHub repository. It takes about 10 minutes the
first time, and the link stays the same afterwards.

1. **Get the code onto `main`.** On GitHub, open a pull request from
   `claude/jolly-knuth-hfonp0` into `main` and merge it, the same way as
   pull request #1.
2. **Create a Render account.** Go to [render.com](https://render.com), click
   **Get Started**, and sign up with your GitHub account.
3. **Create the app.** In the Render dashboard click **New** →
   **Blueprint**. Choose the `Air-Traffic-Control` repository. If it isn't
   listed, click **Configure GitHub** / **Connect account** and give Render
   access to it. Render finds the `render.yaml` file in the repository and
   shows one service, **air-traffic-control**, on the **Free** plan.
4. **Deploy.** Click **Deploy Blueprint** (or **Apply**). The first build
   takes a few minutes; wait until the service says **Live**.
5. **Get your link.** Open the **air-traffic-control** service. Its address
   is at the top, like `https://air-traffic-control-abcd.onrender.com`.
   Open it: the start page shows the **Pilot link** (ending in `/cockpit`)
   and the **Control tower link**, each with a Copy button. Send the pilot
   link to your testers.

Good to know about the free plan:

- If nobody has used the site for about 15 minutes, it goes to sleep. The
  next visitor waits up to a minute while it wakes up; after that it's fast.
- Waking up or redeploying starts a fresh game: planes and scores reset.
- Every time you push to `main`, Render redeploys the new version
  automatically.
- Anyone with the tower link can pause the game or add aircraft.

### Quick alternative: share from your own computer

For a quick test without an account, Cloudflare's free "quick tunnel" gives
the server running on your computer a temporary public link. It only works
while your computer and the server are running.

1. Install `cloudflared`: on Windows run `winget install --id Cloudflare.cloudflared`;
   on Mac run `brew install cloudflared`.
2. Start the game server as below (`python AirTrafficMain.py`).
3. In a second terminal, run `cloudflared tunnel --url http://localhost:8000`.
4. It prints a link like `https://some-random-words.trycloudflare.com`.
   Send pilots that link with `/cockpit` added to the end. You get a new
   link each time you run it.

## Running it on your computer

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
activate the environment and run `python AirTrafficMain.py`. Keep that
terminal open while you play; press Ctrl+C to stop the server.

Then open `http://localhost:8000`. The start page shows links that work for
**computers on the same network** as yours (same home or office Wi-Fi). For
anyone else, use one of the options in "Share a link with anyone" above.

## If pilots on your network can't connect

- **The start page shows a red warning box.** Do what it says.
- **Windows firewall.** Start → search **"Allow an app through Windows
  Firewall"** → **Change settings** → tick **Private** and **Public** for
  every **Python** entry → OK. Then restart the server.
- **Network set to Public (Windows).** Settings → Network & internet → Wi-Fi
  → your network → set **Network profile type** to **Private**.
- **Guest, school or office networks** often stop computers from reaching
  each other. Put the server online instead (see above).
- **VPN.** Turn off any VPN on both computers.
