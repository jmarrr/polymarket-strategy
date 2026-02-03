# Deploying Polymarket Sniper to DigitalOcean

## Step 1: Create DigitalOcean Account & Droplet

1. Go to https://www.digitalocean.com
2. Create account, verify email, add payment method
3. Click "Create" → "Droplets"
4. Configure:
   - **Region**: New York (NYC1/NYC3) — closest to Polymarket
   - **Image**: Ubuntu 24.04
   - **Size**: Basic → Regular → $4/mo (1 vCPU, 512MB RAM) or $6/mo (1GB RAM)
   - **Authentication**: SSH Key (recommended) or Password
5. Click "Create Droplet"
6. Note the IP address

## Step 2: Connect to Server

```bash
ssh root@YOUR_SERVER_IP
```

## Step 3: Run Setup Script

```bash
# Download and run setup
curl -sSL https://raw.githubusercontent.com/jmarrr/polymarket-strategy/master/setup.sh | bash
```

Or manually:

```bash
# Update system
apt update && apt upgrade -y

# Install Python 3.12+ (required for claim script)
apt install -y software-properties-common
add-apt-repository -y ppa:deadsnakes/ppa
apt update
apt install -y python3.12 python3.12-venv python3-pip git

# Clone repository
git clone https://github.com/jmarrr/polymarket-strategy.git
cd polymarket-strategy

# Create virtual environment for sniper
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Step 4: Configure Environment

```bash
cd ~/polymarket-strategy

# Create .env file
cat > .env << 'EOF'
PRIVATE_KEY=your_private_key_here
FUNDER_ADDRESS=your_funder_address_here
EOF

# Secure the file
chmod 600 .env
```

## Step 5: Test Run

```bash
cd ~/polymarket-strategy
source venv/bin/activate
python sniper.py
```

Verify it connects and shows prices. Press Ctrl+C to stop.

## Step 6: Install as Service (Auto-restart)

```bash
# Copy service file
sudo cp sniper.service /etc/systemd/system/

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable sniper
sudo systemctl start sniper

# Check status
sudo systemctl status sniper
```

## Step 7: Setup Auto-Claim (Optional but Recommended)

The claim script redeems winning positions so capital can be recycled. Setup runs automatically via setup.sh, but to configure manually:

```bash
# Create separate venv for claim script (requires Python 3.12+)
cd ~/polymarket-strategy
python3.12 -m venv venv_claim
source venv_claim/bin/activate
pip install -r requirements_claim.txt
deactivate

# Test claim script
~/polymarket-strategy/venv_claim/bin/python claim.py

# Add cron job (runs every hour)
crontab -e
# Add this line:
0 * * * * cd ~/polymarket-strategy && ~/polymarket-strategy/venv_claim/bin/python claim.py >> logs/claim.log 2>&1
```

## Step 8: Monitor

```bash
# View live sniper logs
journalctl -u sniper -f

# View trade log
tail -f ~/polymarket-strategy/logs/trades.log

# View claim log
tail -f ~/polymarket-strategy/logs/claim.log

# Restart service
sudo systemctl restart sniper

# Stop service
sudo systemctl stop sniper
```

## Updating the Bot

```bash
cd ~/polymarket-strategy
git pull
sudo systemctl restart sniper
```

## Troubleshooting

### Service won't start
```bash
journalctl -u sniper -n 50 --no-pager
```

### Check if Python environment is correct
```bash
source ~/polymarket-strategy/venv/bin/activate
python -c "import py_clob_client; print('OK')"
```

### Claim script issues
```bash
# Check Python version (needs 3.12+)
python3.12 --version

# Test claim script manually
cd ~/polymarket-strategy
source venv_claim/bin/activate
python claim.py

# Check cron is running
crontab -l
```

### Firewall issues
```bash
# DigitalOcean firewall is managed via dashboard, but if using ufw:
ufw allow ssh
ufw enable
```

### Out of memory (512MB droplet)
```bash
# Add swap space
fallocate -l 1G /swapfile
chmod 600 /swapfile
mkswap /swapfile
swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
```
