"""
Web Dashboard for Polymarket Sniper

Provides real-time status monitoring via web browser.
Access at http://localhost:5000
"""

import threading
from flask import Flask, render_template, jsonify

# Import the shared dashboard data from sniper
from sniper import get_dashboard_data, _dashboard_data

app = Flask(__name__)


@app.route('/')
def index():
    """Serve the main dashboard page."""
    return render_template('dashboard.html')


@app.route('/api/status')
def api_status():
    """API endpoint for real-time status updates."""
    return jsonify(get_dashboard_data())


def run_dashboard(host='0.0.0.0', port=5000):
    """Run the dashboard server."""
    print(f"\n{'='*50}")
    print(f"  Dashboard running at http://{host}:{port}")
    print(f"{'='*50}\n")
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == '__main__':
    run_dashboard()
