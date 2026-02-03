#!/bin/bash
set -e

echo "🚀 Setting up Polymarket Sniper..."

# Update system
apt update && apt upgrade -y

# Install Python 3.12+ (required for claim script)
apt install -y software-properties-common
add-apt-repository -y ppa:deadsnakes/ppa
apt update
apt install -y python3.12 python3.12-venv python3-pip git

# Clone repo
cd ~
git clone https://github.com/jmarrr/polymarket-strategy.git
cd polymarket-strategy

# Setup sniper venv (Python 3.x)
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
deactivate

# Setup claim venv (Python 3.12 required)
python3.12 -m venv venv_claim
source venv_claim/bin/activate
pip install --upgrade pip
pip install -r requirements_claim.txt
deactivate

# Create logs directory
mkdir -p logs

# Install systemd service
cp sniper.service /etc/systemd/system/
systemctl daemon-reload

# Setup cron job for claim script (every hour)
(crontab -l 2>/dev/null; echo "0 * * * * cd ~/polymarket-strategy && ~/polymarket-strategy/venv_claim/bin/python claim.py >> logs/claim.log 2>&1") | crontab -

echo ""
echo "✅ Setup complete!"
echo ""
echo "Next steps:"
echo "1. Create .env file with your keys:"
echo "   cd ~/polymarket-strategy"
echo "   nano .env"
echo ""
echo "2. Add these lines:"
echo "   PRIVATE_KEY=your_private_key"
echo "   FUNDER_ADDRESS=your_funder_address"
echo ""
echo "3. Start the bot:"
echo "   systemctl start sniper"
echo ""
echo "4. Check status:"
echo "   systemctl status sniper"
echo "   journalctl -u sniper -f"
echo ""
echo "5. Claim script runs automatically every hour via cron"
echo "   Check logs: tail -f ~/polymarket-strategy/logs/claim.log"
