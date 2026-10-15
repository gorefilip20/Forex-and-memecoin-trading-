# Hostinger VPS deployment runbook

This project is configured for **paper mode by default**. The runbook does not connect a broker, fund an account, or activate live orders.

## Architecture

- `api`: FastAPI dashboard, signals, status, and authenticated control endpoints.
- `worker`: optional persistent process for background loops; enable only after paper/demo validation.
- `redis`: persistent append-only state store.
- MT5: runs separately on a VPS desktop/RDP session with the EA installed and pointed at the API.

Hostinger documents MT5 on an Ubuntu 24.04 desktop VPS using XFCE/RDP. The Docker Compose stack is intended for the API and Redis, not for hiding or automating broker credentials.

## Safe deployment

```bash
cp .env.hostinger.example .env.hostinger
# Edit only paper/demo settings first.
docker compose -f docker-compose.hostinger.yml up -d api redis
docker compose -f docker-compose.hostinger.yml ps
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/readiness
```

Keep port 8000 behind a reverse proxy or firewall. Do not expose Redis publicly. Put the dashboard behind authentication before publishing it.

## Promotion gates

1. **Research:** no execution; validate historical data and costs.
2. **Paper:** `DEPLOYMENT_MODE=paper`, all live flags false.
3. **Demo:** use an MT5 demo account and confirm broker-side SL/TP, fills, retries, restart recovery, reconciliation, and daily loss lock.
4. **Canary:** only after independent review; use a deliberately small amount and `APPROVE_FIRST=true`.
5. **Live:** only after the demo and canary acceptance records are complete. This project does not activate this stage automatically.

At every stage, `/readiness` must be green and the trade ledger must reconcile. Any mismatch must pause new entries.

## Operational requirements

- Enable VPS firewall; expose only SSH, HTTPS, and RDP as necessary.
- Use SSH keys, disable password SSH, and create a non-root deployment user.
- Keep `.env.hostinger` outside Git and back it up securely.
- Use a dedicated demo/live wallet or account with limited funds; never commit seed phrases or private keys.
- Configure Telegram/Discord alerts for crashes, stale data, broker disconnects, reconciliation mismatches, and loss-limit pauses.
- Review Redis persistence and restore procedures before demo trading.
- Do not run two workers against the same trading namespace without a distributed lock.

## Acceptance evidence required before considering live

- At least 100 validation trades after realistic fees and slippage.
- Positive net expectancy in untouched out-of-sample data.
- Walk-forward results that remain positive under stressed costs.
- No duplicate order in retry/concurrency tests.
- No unreconciled MT5 position after restart or network outage simulation.
- Correct stop, target, volume, and ticket on every demo fill.
- Tested daily drawdown and emergency pause.
- Independent review of the complete ledger.

These criteria cannot guarantee profit. They are designed to reject fragile or operationally unsafe deployments.
