# Deploying polyfin

Everything runs on one box: the TimescaleDB container (127.0.0.1:5446), and
systemd units for the recorder, the paper trader, the live trader and a daily
stage 2 refit. Logs go to journald.

| unit | what | enabled by bootstrap |
|---|---|---|
| `polyfin-recorder` | markets, 1m bars, price history, books | yes |
| `polyfin-weekly` | weekly touch markets (`polyfin/weekly/`) - **runs on the laptop**, not here: the server disk is shared | no |
| `polyfin-paper` | paper trader - keeps running beside live as its twin | yes |
| `polyfin-refit.timer` | refits `data/stage2_params.json` 07:30 UTC; traders reload it | yes |
| `polyfin-live` | trader with live authority | **no** |

## First install

```sh
sudo mkdir -p /opt/polyfin && sudo chown $USER: /opt/polyfin
git clone <repo> /opt/polyfin && cd /opt/polyfin
deploy/bootstrap.sh
# ~4 minutes later (recorder backfill done):
.venv/bin/python -m polyfin.stage2
```

## Going live (the $3 test)

1. **Wallet** - a new Polymarket account used only by polyfin (signature type 3).
   Put `FIN_PRIVATE_KEY` and `FIN_DEPOSIT_WALLET` in `.env`. Fund the deposit
   wallet with pUSD. For the redeem fallback also set `FIN_RELAYER_API_KEY`,
   `FIN_RELAYER_API_KEY_ADDRESS` (polymarket.com/settings) and a paid
   `FIN_POLYGON_RPC_URL`.
2. **API credentials** - `.venv/bin/python scripts/derive_api_creds.py --write`
3. **Approvals** - `.venv/bin/python scripts/approve.py` (dry run), then `--send`
   if anything is missing. Website onboarding may already have set them.
4. **Preflight** - `.venv/bin/python scripts/preflight.py` must say *all checks
   passed*. It signs an order locally and posts nothing.
5. **Start** - both keys are needed; either alone trades nothing live:
   ```sh
   sudo systemctl enable --now polyfin-live      # launch authority (--live)
   .venv/bin/python -m polyfin.live.control live "first \$3 test"
   ```
6. **Watch** - `journalctl -fu polyfin-live`, and
   `.venv/bin/python -m polyfin.live.report --mode live` next to `--mode paper`.
   Live trades the early slot only (`MODE_SLOTS`); paper keeps both slots. The
   stop-after-fills gate is off (`STOP_AFTER_FILLS_LIVE = None`) - pause on request.

Stop at any time: `.venv/bin/python -m polyfin.live.control pause "why"` (stops
new entries within 30s; reconciliation and redemption keep running).

## Updates

`git push` from the laptop, then `deploy/update.sh` on the box.

## Moving the laptop's data (optional)

The recorder backfills 7 days on its own; only paper history would be lost:

```sh
# laptop
docker exec polyfin-timescaledb pg_dump -U polyfin -Fc polyfin > polyfin.dump
# box
docker exec -i polyfin-timescaledb pg_restore -U polyfin -d polyfin --clean < polyfin.dump
```
