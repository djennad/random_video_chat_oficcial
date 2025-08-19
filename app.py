"""
Random Video Chat using Python (Flask) + WebRTC — **No Socket.IO / No ssl required**
Single-file app that avoids the `ssl` import issue by replacing Socket.IO with
Server-Sent Events (SSE) + simple HTTP endpoints for signaling.

Why this rewrite?
- You hit `ModuleNotFoundError: No module named 'ssl'` when importing Flask-SocketIO.
- Some sandboxed/custom Python builds omit the stdlib `ssl` module, and the
  `python-engineio/socketio` stack imports it at import time.
- This version removes that dependency and should run anywhere plain Flask runs.

Quick start:
  1) python -m venv venv && source venv/bin/activate   # (Windows: venv\Scripts\activate)
  2) pip install flask
  3) python app.py
  4) Open http://127.0.0.1:5000 in TWO browser tabs to test.

Notes:
- Uses public Google STUN for NAT traversal. For Internet usage behind strict NATs, add TURN.
- Minimal demo (no auth/rate limits). For production, add persistence, auth, abuse prevention, and a TURN server.
- Includes a tiny test suite you can run with:  `python app.py --test`
"""

from __future__ import annotations
import os
import json
import time
import uuid
from typing import Dict, List, Optional
from queue import Queue, Empty
from dataclasses import dataclass, field

from flask import Flask, Response, request, jsonify

# -------------------- App & In-Memory State --------------------
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret')

# Simple matchmaking & signaling state
waiting_id: Optional[str] = None  # single waiting user (super-simple queue)
rooms: Dict[str, Dict[str, str]] = {}            # room_id -> {"a": id, "b": id}
peer_of: Dict[str, str] = {}                     # client_id -> peer_id
room_of: Dict[str, str] = {}                     # client_id -> room_id
message_queues: Dict[str, Queue] = {}            # client_id -> Queue of outbound events


def _ensure_queue(cid: str) -> Queue:
    q = message_queues.get(cid)
    if q is None:
        q = Queue()
        message_queues[cid] = q
    return q


def _enqueue(cid: str, event: str, data: dict):
    if cid in message_queues:
        message_queues[cid].put({"event": event, "data": data})


def _leave_current_room(cid: str, notify_peer: bool = False):
    """Remove a client from its room and optionally notify their peer."""
    rid = room_of.pop(cid, None)
    if not rid:
        # If they were waiting, clear that too
        global waiting_id
        if waiting_id == cid:
            waiting_id = None
        return

    other = peer_of.pop(cid, None)
    if other:
        peer_of.pop(other, None)

    members = rooms.get(rid, {})
    for k, v in list(members.items()):
        if v == cid:
            del members[k]
    rooms[rid] = members

    if notify_peer and other:
        _enqueue(other, 'peer-left', {})

    if not members:
        rooms.pop(rid, None)


# -------------------- HTTP Routes (REST + SSE) --------------------

@app.post('/register')
def register():
    """Create a new logical client ID and queue."""
    cid = str(uuid.uuid4())
    _ensure_queue(cid)
    return jsonify({"id": cid})


@app.post('/find')
def find_partner():
    """Either pair with waiting client or mark this client as waiting."""
    global waiting_id
    cid = request.json.get('id')
    if not cid:
        return jsonify({"error": "missing id"}), 400

    # If already in a room, leave it first
    if cid in room_of:
        _leave_current_room(cid)

    # If someone is already waiting, pair them
    if waiting_id and waiting_id != cid:
        other = waiting_id
        waiting_id = None
        rid = str(uuid.uuid4())
        rooms[rid] = {"a": other, "b": cid}
        peer_of[other] = cid
        peer_of[cid] = other
        room_of[other] = rid
        room_of[cid] = rid
        # Assign roles deterministically: waiting user becomes 'caller'
        _enqueue(other, 'matched', {"room": rid, "role": "caller", "peer": cid})
        _enqueue(cid,   'matched', {"room": rid, "role": "callee", "peer": other})
        return jsonify({"status": "matched", "room": rid})

    # Otherwise, this client becomes waiting
    waiting_id = cid
    _enqueue(cid, 'status', {"msg": "Searching for a partner…"})
    return jsonify({"status": "waiting"})


@app.post('/signal')
def signal():
    """Forward SDP/ICE to a specific peer via their queue."""
    cid = request.json.get('id')
    to = request.json.get('to')
    typ = request.json.get('type')
    payload = request.json.get('payload')
    if not all([cid, to, typ]):
        return jsonify({"error": "missing fields"}), 400
    if to not in message_queues:
        return jsonify({"error": "peer not found"}), 404
    _enqueue(to, 'signal', {"from": cid, "type": typ, "payload": payload})
    return jsonify({"ok": True})


@app.post('/next')
def next_partner():
    cid = request.json.get('id')
    if not cid:
        return jsonify({"error": "missing id"}), 400
    _leave_current_room(cid, notify_peer=True)
    return find_partner()


@app.post('/leave')
def leave():
    cid = request.json.get('id')
    if not cid:
        return jsonify({"error": "missing id"}), 400
    _leave_current_room(cid, notify_peer=True)
    _enqueue(cid, 'status', {"msg": "Left the chat."})
    return jsonify({"ok": True})


@app.get('/events')
def events():
    """SSE event stream for a client. Client connects to /events?id=..."""
    cid = request.args.get('id')
    if not cid:
        return jsonify({"error": "missing id"}), 400
    q = _ensure_queue(cid)

    def gen():
        # Send a hello so the stream is considered open
        yield "event: status\ndata: {\"msg\": \"Connected to SSE.\"}\n\n"
        while True:
            try:
                item = q.get(timeout=60)
            except Empty:
                # Keep-alive comment to prevent proxies from closing
                yield ": keep-alive\n\n"
                continue
            try:
                payload = json.dumps(item["data"])  # ensure JSON serialization
            except Exception:
                payload = json.dumps({"error": "serialization"})
            yield f"event: {item['event']}\ndata: {payload}\n\n"
    return Response(gen(), mimetype='text/event-stream')


# -------------------- Inline Client (HTML/JS) --------------------
HTML_PAGE = r"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Random Video Chat — Flask + WebRTC (SSE Signaling)</title>
  <style>
    :root { font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, "Helvetica Neue", Arial; }
    body { margin: 0; display: grid; min-height: 100vh; grid-template-rows: auto 1fr auto; }
    header { padding: 12px 16px; border-bottom: 1px solid #ddd; display: flex; gap: 12px; align-items: center; }
    main { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; padding: 8px; }
    video { width: 100%; background: #000; aspect-ratio: 16/9; border-radius: 12px; }
    footer { padding: 10px 16px; border-top: 1px solid #eee; color: #555; font-size: 14px; }
    button { padding: 10px 14px; border: 0; border-radius: 10px; cursor: pointer; font-weight: 600; }
    #findBtn { background: #2563eb; color: white; }
    #nextBtn { background: #10b981; color: white; }
    #leaveBtn { background: #ef4444; color: white; }
    #status { margin-left: auto; font-weight: 500; }
    @media (max-width: 900px){ main { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
  <header>
    <button id="findBtn">Find Match</button>
    <button id="nextBtn" disabled>Next</button>
    <button id="leaveBtn" disabled>Leave</button>
    <div id="status">Idle</div>
  </header>
  <main>
    <div>
      <h3>Your Camera</h3>
      <video id="localVideo" autoplay playsinline muted></video>
    </div>
    <div>
      <h3>Partner</h3>
      <video id="remoteVideo" autoplay playsinline></video>
    </div>
  </main>
  <footer>
    Minimal demo using SSE for signaling. Add a TURN server for reliability across NATs.
  </footer>

  <script>
    let clientId = null;
    let peerId = null;
    let es = null;

    const localVideo = document.getElementById('localVideo');
    const remoteVideo = document.getElementById('remoteVideo');
    const findBtn = document.getElementById('findBtn');
    const nextBtn = document.getElementById('nextBtn');
    const leaveBtn = document.getElementById('leaveBtn');
    const statusEl = document.getElementById('status');

    let pc = null;
    let localStream = null;
    let role = null; // 'caller' or 'callee'
    let roomId = null;

    const iceServers = [{ urls: 'stun:stun.l.google.com:19302' }];

    function setStatus(msg){ statusEl.textContent = msg; }
    function setConnectedState(connected){
      findBtn.disabled = connected;
      nextBtn.disabled = !connected;
      leaveBtn.disabled = !connected;
    }

    async function api(path, body){
      const res = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
      if (!res.ok) throw new Error('API error ' + res.status);
      return res.json();
    }

    async function startLocal(){
      if (localStream) return;
      try {
        localStream = await navigator.mediaDevices.getUserMedia({ video: true, audio: true });
        localVideo.srcObject = localStream;
      } catch (err){
        console.error(err);
        alert('Could not access camera/microphone: ' + err.message);
      }
    }

    function createPeer(){
      pc = new RTCPeerConnection({ iceServers });
      localStream.getTracks().forEach(t => pc.addTrack(t, localStream));

      pc.onicecandidate = (e) => {
        if (e.candidate) {
          api('/signal', { id: clientId, to: peerId, type: 'candidate', payload: e.candidate });
        }
      };
      pc.ontrack = (e) => {
        remoteVideo.srcObject = e.streams[0];
      };
      pc.onconnectionstatechange = () => {
        setStatus('Peer state: ' + pc.connectionState);
      };
    }

    async function makeOffer(){
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      await api('/signal', { id: clientId, to: peerId, type: 'offer', payload: offer });
    }

    async function makeAnswer(offer){
      await pc.setRemoteDescription(new RTCSessionDescription(offer));
      const answer = await pc.createAnswer();
      await pc.setLocalDescription(answer);
      await api('/signal', { id: clientId, to: peerId, type: 'answer', payload: answer });
    }

    function cleanup(){
      if (pc){ pc.ontrack = null; pc.onicecandidate = null; pc.close(); pc = null; }
      if (remoteVideo.srcObject){ remoteVideo.srcObject.getTracks().forEach(t => t.stop()); }
      remoteVideo.srcObject = null;
      roomId = null; role = null; peerId = null;
      setConnectedState(false);
    }

    function listen(){
      if (es) es.close();
      es = new EventSource('/events?id=' + clientId);
      es.addEventListener('status', (ev) => {
        try { const d = JSON.parse(ev.data); setStatus(d.msg || ''); } catch {}
      });
      es.addEventListener('matched', async (ev) => {
        const d = JSON.parse(ev.data);
        roomId = d.room; role = d.role; peerId = d.peer;
        setConnectedState(true);
        await startLocal();
        createPeer();
        if (role === 'caller') {
          await makeOffer();
        }
      });
      es.addEventListener('signal', async (ev) => {
        const s = JSON.parse(ev.data);
        if (!pc) return;
        if (s.type === 'offer') {
          await makeAnswer(s.payload);
        } else if (s.type === 'answer') {
          await pc.setRemoteDescription(new RTCSessionDescription(s.payload));
        } else if (s.type === 'candidate') {
          try { await pc.addIceCandidate(new RTCIceCandidate(s.payload)); }
          catch (e) { console.warn('Bad ICE candidate', e); }
        }
      });
      es.addEventListener('peer-left', () => {
        setStatus('Partner disconnected. Click Find to match again.');
        cleanup();
      });
    }

    // UI events
    findBtn.onclick = async () => {
      await startLocal();
      setStatus('Looking for a partner…');
      await api('/find', { id: clientId });
    };

    nextBtn.onclick = async () => {
      cleanup();
      setStatus('Searching for a new partner…');
      await api('/next', { id: clientId });
    };

    leaveBtn.onclick = async () => {
      await api('/leave', { id: clientId });
      cleanup();
      setStatus('Left the chat.');
    };

    window.addEventListener('beforeunload', () => { navigator.sendBeacon('/leave', JSON.stringify({ id: clientId })); });

    // Boot
    (async function init(){
      const r = await api('/register');
      clientId = r.id;
      listen();
      setStatus('Ready. Click Find Match.');
    })();
  </script>
</body>
</html>
"""


@app.get('/')
def index() -> Response:
    return Response(HTML_PAGE, mimetype='text/html')


# -------------------- Test Utilities (for unit tests) --------------------
# These debug endpoints help validate server behavior without needing a browser.
# They are enabled by default; disable in production with ENABLE_TEST_ROUTES=0

def tests_enabled() -> bool:
    return os.environ.get('ENABLE_TEST_ROUTES', '1') == '1'


@app.post('/_test/pull')
def _test_pull():
    if not tests_enabled():
        return jsonify({"error": "disabled"}), 403
    cid = request.json.get('id')
    if not cid:
        return jsonify({"error": "missing id"}), 400
    q = _ensure_queue(cid)
    items: List[dict] = []
    try:
        while True:
            items.append(q.get_nowait())
    except Exception:
        pass
    return jsonify(items)


# -------------------- Unit Tests --------------------
import unittest

class AppTests(unittest.TestCase):
    def setUp(self):
        # Reset in-memory state between tests
        global waiting_id, rooms, peer_of, room_of, message_queues
        waiting_id = None
        rooms = {}
        peer_of = {}
        room_of = {}
        message_queues = {}
        self.c = app.test_client()

    def register(self):
        r = self.c.post('/register')
        self.assertEqual(r.status_code, 200)
        return r.get_json()['id']

    def pull(self, cid):
        r = self.c.post('/_test/pull', json={'id': cid})
        self.assertEqual(r.status_code, 200)
        return r.get_json()

    def test_match_two_clients_and_signal(self):
        a = self.register(); b = self.register()
        self.c.post('/find', json={'id': a})
        self.c.post('/find', json={'id': b})
        # Both should receive a 'matched' event
        ev_a = self.pull(a)
        ev_b = self.pull(b)
        self.assertTrue(any(e['event']=='matched' for e in ev_a))
        self.assertTrue(any(e['event']=='matched' for e in ev_b))
        # Send a fake offer from a -> b and ensure b receives it
        self.c.post('/signal', json={'id': a, 'to': b, 'type': 'offer', 'payload': {'sdp': 'x'}})
        ev_b2 = self.pull(b)
        self.assertTrue(any(e['event']=='signal' and e['data']['type']=='offer' for e in ev_b2))

    def test_leave_notifies_peer(self):
        a = self.register(); b = self.register()
        self.c.post('/find', json={'id': a})
        self.c.post('/find', json={'id': b})
        self.pull(a); self.pull(b)  # drain matched
        self.c.post('/leave', json={'id': a})
        ev_b = self.pull(b)
        self.assertTrue(any(e['event']=='peer-left' for e in ev_b))

    def test_waiting_status(self):
        a = self.register()
        self.c.post('/find', json={'id': a})
        ev_a = self.pull(a)
        self.assertTrue(any(e['event']=='status' for e in ev_a))


# -------------------- Main --------------------
if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=int(os.environ.get('PORT', 5000)))
    parser.add_argument('--test', action='store_true', help='run unit tests and exit')
    args = parser.parse_args()

    if args.test:
        # Disable SSE streaming during tests, only queue-based checks
        os.environ['ENABLE_TEST_ROUTES'] = '1'
        unittest.main(argv=['ignored'], exit=False)
    else:
        # Use Flask built-in server; SSE works without extra deps and no ssl module.
        app.run(host=args.host, port=args.port, threaded=True, debug=False)
