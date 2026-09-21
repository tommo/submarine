# Submarine web console — remote access

The console is plain HTTP, and a prompt is arbitrary work on your machine.
It is meant to be reached over a **private network you already trust** — a
VPN or overlay (WireGuard, ZeroTier, Tailscale, …) or an SSH tunnel — never
over the open internet.

## Pattern

1. Put the machine running Sublime and the device you browse from on the
   same private network.
2. Bind the console to that network's address only, with a token:

   ```bash
   python3 submarine_web.py --host <private-address> --token <secret>
   ```

   Only members of that network can reach the port.
3. Open `http://<private-address>:8787/?token=<secret>` once; the page keeps
   the token and drops it from the URL. Add to Home Screen for an app tile.

An SSH tunnel is the same pattern with `--host 127.0.0.1` and
`ssh -L 8787:127.0.0.1:8787 <machine>` from the client.

## What not to do

- Do not port-forward the router to it, with or without TLS.
- A public tunnel (Cloudflare Tunnel, ngrok) needs real authentication in
  front of it; the shared token alone is not enough for an internet-facing
  port.

## What the token is

- A shared secret checked (constant time) on every `/api` route, as the
  `X-Submarine-Token` header or `?token=`. The static page is not gated; it
  holds nothing.
- No lockout, no rate limit, no TLS: it travels in clear text, so the
  network underneath has to be the private one.
