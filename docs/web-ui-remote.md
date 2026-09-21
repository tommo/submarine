# Submarine web console — remote access

The console is plain HTTP and a prompt is arbitrary work on your machine, so
it never faces the internet directly. It rides an overlay network: the phone
and the Mac share a private address space, the console binds to that address
only, and the overlay does the encryption and the "who is this device" part.

## ZeroTier

1. Mac and phone join the same ZeroTier network; authorise both in the
   network's member list. Each gets a managed address (say `10.147.20.5` for
   the Mac).
2. Bind the console to that address only:

   ```bash
   python3 submarine_web.py --host 10.147.20.5 --token <secret>
   ```

   Nothing else on the LAN or the internet can reach the port; only members
   of the network can.
3. On the phone open `http://10.147.20.5:8787/?token=<secret>` once — the page
   remembers the token and drops it from the URL. Add to Home Screen for an
   app tile.

The `--token` is a second factor on top of ZeroTier's membership. The
console holds no data of its own; everything goes to the plugin socket.

Bind by interface instead of address if the managed IP changes:

```bash
python3 submarine_web.py --host "$(ipconfig getifaddr "$(ifconfig | awk '/^zt/{print $1}' | tr -d : | head -1)")"
```

(`zt*` is the ZeroTier interface on macOS/Linux.) Keep the console
running with `launchd` / `nohup` or a tmux window; it reconnects to the plugin
socket on every request, so Sublime can restart underneath it.

## Alternatives

| | how | what the console needs |
|---|---|---|
| Tailscale / WireGuard | same shape as ZeroTier: bind the `100.x` / tunnel address | `--host <addr> --token` |
| SSH tunnel | `ssh -L 8787:127.0.0.1:8787 mac` from Termius / Blink | `--host 127.0.0.1` |
| Cloudflare Tunnel / ngrok | a public HTTPS URL | put real auth in front (Cloudflare Access); the shared token alone is not enough for an internet-facing port |

Do not port-forward the router to it, with or without TLS.

## What the token does and does not do

- Gates every `/api` route (`X-Submarine-Token` header or `?token=`); the
  static page itself is not gated — it holds nothing.
- Compared in constant time; there is no lockout or rate limit.
- Travels in clear text over the overlay (which is itself encrypted); there is
  no TLS in the console. On a plain LAN without an overlay, anyone on the
  network can read it.
