---
name: printer
description: The owner's 3D printer (Bambu Lab, at another site): current print progress, ETA, temperatures, filament in the AMS, errors, and the history of past prints. Read-only.
---

# 3D printer (read-only)

`~/bin/printer` reads the printer through Bambu Cloud, the same data the owner's phone app shows.
**There is no control:** no pause, stop, start or temperature changes. The owner does that in the
Bambu app. Never look for a way around this. Which printer it is and where it stands: `~/assistant/MAP.md`.

| Question | Command |
| --- | --- |
| How is the print going / is it done? | `printer status` (`--json` for every field) |
| What happened during the last prints (started, paused, failed, errors)? | `printer events 50` |
| Past jobs: name, duration, filament weight | `printer history 20` (from Bambu Cloud, also covers time before the watcher ran) |
| Something odd in the data | `~/printer/state.json` → `raw`: the full merged report from the printer |

- `data: N min ago` is how fresh the status is. Reports stop while the printer is off or offline; old data
  with `watcher: connected` usually means the printer is off.
- Errors: `HMS_xxxx_xxxx_xxxx_xxxx` codes are explained on Bambu's wiki (search the code at
  wiki.bambulab.com); `print_error xxxx_xxxx` at https://e.bambulab.com/query.php?lang=en. Look them up
  before guessing.
- Troubleshooting a print: combine the events of that job (layer and time of the first error) with
  what the owner and you know about the file, filament and settings.
- `token: … renew soon` or `watcher: NOT connected (TOKEN-EXPIRED…)`: tell the owner. They run
  `ssh -t personal printer login` themselves (password and codes never go through chat), then
  `sudo systemctl restart printer-watch`.
- Watcher health: `systemctl status printer-watch`, `journalctl -u printer-watch -n 50`.
