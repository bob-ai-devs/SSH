# SSH Console (Streamlit)

Connect to any SSH server from the browser: terminal-style command runner, SFTP file browser
(preview / download / upload) and a server info tab.

## Run locally
```bash
pip install -r requirements.txt
streamlit run app.py
```

## Deploy on Streamlit Community Cloud
1. Push this folder to a GitHub repo (`app.py`, `requirements.txt`, `.streamlit/config.toml`).
2. share.streamlit.io -> **New app** -> pick the repo, branch, main file `app.py`.
3. (Recommended) **Settings -> Secrets**:
```toml
APP_PASSWORD  = "choose-a-strong-password"      # gate the whole app
ALLOWED_HOSTS = "myserver.example.com,203.0.113.10"   # optional allow-list
```
4. Set the app to **private** (Settings -> Sharing) so only invited emails can open it.

## Limits
- Streamlit Cloud connects from the public internet: servers on a private network/VPN, or ones that
  firewall unknown IPs, are unreachable.
- Commands run without a TTY; interactive programs (vim, nano, top, ssh prompts) don't work.
- Host keys are auto-accepted on first sight.
