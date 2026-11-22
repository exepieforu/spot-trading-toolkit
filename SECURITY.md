# Security

## Protecting your Binance account
- Create API keys with **Enable Spot & Margin Trading only**. **Never enable withdrawals.**
- Restrict each key to the IP address of the machine running the bot.
- Keys live only in a local `.env` file, which is git-ignored. Never paste keys into issues,
  pull requests, screenshots or logs. If a key may have leaked, delete it in Binance immediately.
- The signal predictor needs **no keys at all**.

## Local dashboards
Both dashboards bind to `127.0.0.1` only, reject requests whose `Host` header is not localhost
(DNS-rebinding protection) and require a per-session secret token on every API call (CSRF protection).
Do not expose them to the internet or a shared network without adding your own authentication.

## Reporting a vulnerability
Please do not open a public issue for security problems. Use GitHub's private
"Report a vulnerability" option on the Security tab of this repository.
