# Deploying Polymarket Sniper to DigitalOcean

## Step 1: Create DigitalOcean Account & Droplet

1. Go to https://www.digitalocean.com
2. Create account, verify email, add payment method
3. Click "Create" → "Droplets"
4. Configure:
   - **Region**: New York (NYC1/NYC3) — closest to Polymarket
   - **Image**: Ubuntu 24.04
   - **Size**: Basic → Regular → $6/mo (1GB RAM recommended)
   - **Authentication**: SSH Key (recommended) or Password
5. Click "Create Droplet"
6. Note the IP address

## Step 2: Connect to Server

```bash
ssh root@YOUR_SERVER_IP
```

## Step 3: Install Dependencies

```bash
# Update system
apt update && apt upgrade -y

# Install required packages
apt install -y python3 python3-pip python3-venv git tmux

# Clone repository
git clone https://github.com/YOUR_REPO/polymarket.git
cd polymarket

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install Python dependencies
pip install -r requirements.txt
```

## Step 4: Configure Environment

```bash
cd ~/polymarket

# Create .env file
nano .env
```

Add your credentials:
```
PRIVATE_KEY=your_private_key_here
FUNDER_ADDRESS=your_funder_address_here
```

Secure the file:
```bash
chmod 600 .env
```

## Step 5: Open Firewall for Dashboard

In DigitalOcean console:
1. Go to **Networking** → **Firewalls**
2. Click **Create Firewall**
3. Add Inbound Rule:
   - Type: **Custom**
   - Protocol: **TCP**
   - Port: **5000**
   - Sources: **All IPv4** (or your IP for security)
4. Apply firewall to your droplet

## Step 6: Run with tmux

tmux keeps processes running after you disconnect.

```bash
cd ~/polymarket
source venv/bin/activate

# Start tmux session
tmux new -s sniper

# Run sniper (dashboard starts automatically)
python sniper.py

# Detach: Ctrl+B, D
```

The dashboard is built into sniper.py - no separate process needed.

## Step 7: Access Dashboard

Open in browser:
```
http://YOUR_SERVER_IP:5000
```

You'll see:
- Live asset prices (updates every 2 seconds)
- Trade history
- Error log
- Current configuration

## tmux Commands Reference

| Command | Action |
|---------|--------|
| `tmux new -s sniper` | Create new session |
| `tmux attach -t sniper` | Reattach to session |
| `Ctrl+B, D` | Detach (keep running) |
| `Ctrl+B, C` | Create new window |
| `Ctrl+B, N` | Next window |
| `Ctrl+B, P` | Previous window |
| `Ctrl+B, 0-9` | Jump to window number |
| `Ctrl+B, W` | List all windows |

## Updating the Bot

```bash
tmux attach -t sniper

# Stop with Ctrl+C, then:
cd ~/polymarket
git pull
pip install -r requirements.txt

# Restart
python sniper.py
```

## Monitoring

```bash
# Reattach to see live output
tmux attach -t sniper

# View trade log
tail -f ~/polymarket/logs/trades.log

# Check dashboard from browser
http://YOUR_SERVER_IP:5000
```

## Troubleshooting

### Can't connect to dashboard
```bash
# Check sniper is running (dashboard is built-in)
tmux attach -t sniper

# Should see "Dashboard running at http://0.0.0.0:5000" in output
# Check firewall allows port 5000 in DigitalOcean dashboard
```

### WebSocket disconnects
```bash
# Check sniper logs for reconnect messages
tmux attach -t sniper
# Switch to window 0: Ctrl+B, 0

# Sniper auto-reconnects with exponential backoff
```

### Check Python environment
```bash
source ~/polymarket/venv/bin/activate
python -c "import py_clob_client; print('OK')"
python -c "import flask; print('OK')"
```

### Out of memory (add swap)
```bash
fallocate -l 1G /swapfile
chmod 600 /swapfile
mkswap /swapfile
swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
```

### Process died after disconnect
Make sure you detached properly with `Ctrl+B, D` (not just closing terminal).

To check if tmux session exists:
```bash
tmux ls
```

To reattach:
```bash
tmux attach -t sniper
```
