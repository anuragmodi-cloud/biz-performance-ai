import { PipecatClient, RTVIEvent } from '@pipecat-ai/client-js';
import { SmallWebRTCTransport } from '@pipecat-ai/small-webrtc-transport';

export const SERVER_URL = import.meta.env.VITE_SERVER_URL || 'http://localhost:8010';

const ANSWERS_POLL_INTERVAL_MS = 1200;

/**
 * The Munshi voice call widget: connect/mic controls, live transcript, and
 * a mic-level meter -- adapted from kyc-voice-agent's kycClient.js
 * (same @pipecat-ai/client-js + SmallWebRTCTransport wiring), plus polling
 * /session/{id}/answers so the output panel can show each ask's structured
 * calculation-engine result alongside the spoken conversation.
 */
export class VoiceClient {
  constructor(ids, callbacks = {}) {
    this.ids = ids;
    this.callbacks = callbacks;
    this.client = null;
    this.sessionId = null;
    this.isConnected = false;
    this.answersPollTimer = null;
    this.lastAnswerCount = 0;

    this.setupDOM();
    this.setupEventListeners();
  }

  setupDOM() {
    const byId = (id) => (id ? document.getElementById(id) : null);
    this.connectBtn = byId(this.ids.connectBtn);
    this.micBtn = byId(this.ids.micBtn);
    this.micIcon = byId(this.ids.micIcon);
    this.micLabel = byId(this.ids.micLabel);
    this.micStatus = byId(this.ids.micStatus);
    this.micMeterFill = byId(this.ids.micMeterFill);
    this.conversationLog = byId(this.ids.conversationLog);
    this.sessionPill = byId(this.ids.sessionPill);
    this.orb = byId(this.ids.orb);
    this.orbStatus = byId(this.ids.orbStatus);
    this.textAskForm = byId(this.ids.textAskForm);
    this.textAskInput = byId(this.ids.textAskInput);
    this.textAskSend = byId(this.ids.textAskSend);
  }

  setupEventListeners() {
    this.connectBtn.addEventListener('click', () => {
      if (this.isConnected) {
        this.disconnect();
      } else {
        this.connect();
      }
    });

    this.micBtn.addEventListener('click', () => {
      if (this.client) {
        const newState = !this.client.isMicEnabled;
        this.client.enableMic(newState);
        this.updateMicButton(newState);
      }
    });

    this.textAskForm?.addEventListener('submit', (event) => {
      event.preventDefault();
      this.sendTypedText();
    });

    // Tells the bot "user is composing a question here" so its idle-check-in
    // logic doesn't mistake normal typing pauses for a dropped call/mic
    // problem and interrupt with "hello, are you there?" mid-sentence.
    this.textAskInput?.addEventListener('input', () => this.pingTyping());
    this.textAskInput?.addEventListener('focus', () => this.pingTyping());
  }

  // Types a question in as text instead of speaking it, but still gets the
  // bot's reply spoken back through the normal TTS pipeline -- lets the mic
  // be bypassed entirely while keeping voice-only output, and doubles as a
  // fallback when mic capture itself is unreliable on a given machine.
  async sendTypedText() {
    const text = this.textAskInput?.value.trim();
    if (!text || !this.client || !this.isConnected) return;

    this.textAskInput.value = '';
    this.addConversationMessage(text, 'user');
    try {
      // audio_response defaults to true server-side (Pipecat's RTVI
      // send-text handler) -- the reply still comes back as spoken TTS.
      await this.client.sendText(text, { run_immediately: true });
    } catch (error) {
      console.error('sendText error:', error);
    }
  }

  // Throttled (client-side, ~3s) so a fast typist doesn't fire a request
  // per keystroke -- the server's grace window (8s) is comfortably wider
  // than this gap, so a couple of dropped/delayed pings are harmless.
  pingTyping() {
    if (!this.isConnected || !this.sessionId) return;
    const now = Date.now();
    if (this._lastTypingPingAt && now - this._lastTypingPingAt < 3000) return;
    this._lastTypingPingAt = now;
    fetch(`${SERVER_URL}/session/${this.sessionId}/typing`, { method: 'POST' }).catch(() => {
      // best-effort -- a missed ping just means the next one (or the
      // server's own grace window) covers it
    });
  }

  async connect() {
    try {
      this.connectBtn.disabled = true;
      this.connectBtn.textContent = 'Connecting...';
      this.setOrb('idle', 'Connect ho raha hai...');

      const startResp = await fetch(`${SERVER_URL}/start-session`, { method: 'POST' });
      if (!startResp.ok) throw new Error(`/start-session failed: ${startResp.status}`);
      const { session_id } = await startResp.json();
      this.sessionId = session_id;

      this.client = new PipecatClient({
        transport: new SmallWebRTCTransport(),
        enableMic: true,
        enableCam: false,
        callbacks: {
          onConnected: () => this.onConnected(),
          onDisconnected: () => this.onDisconnected(),
          onBotReady: () => this.setOrb('idle', 'Bolne ke liye taiyar hain — kuch bhi poochiye'),
          onUserStartedSpeaking: () => this.setOrb('listening', 'Sun rahe hain...'),
          onUserStoppedSpeaking: () => this.setOrb('idle', 'Soch rahe hain...'),
          onBotStartedSpeaking: () => this.setOrb('speaking', 'Bol rahe hain...'),
          onBotStoppedSpeaking: () => this.setOrb('idle', 'Sunne ke liye taiyar hain'),
          onUserTranscript: (data) => {
            if (data.final) this.addConversationMessage(data.text, 'user');
          },
          onBotTranscript: (data) => this.addConversationMessage(data.text, 'bot'),
          onError: (error) => console.error('Voice client error:', error),
          // getUserMedia failures (permission denied, mic in use elsewhere,
          // no device found, insecure context) otherwise fail completely
          // silently -- the UI would just sit at "Mic Off" forever with no
          // clue why. This is the one callback that actually tells us.
          onDeviceError: (error) => this.onDeviceError(error),
          onMicUpdated: (mic) => console.info('Mic device acquired:', mic?.label || mic),
        },
      });

      this.setupAudio();

      await this.client.connect({
        webrtcUrl: `${SERVER_URL}/api/offer?session_id=${encodeURIComponent(session_id)}`,
      });
    } catch (error) {
      console.error('Connection error:', error);
      this.setOrb('idle', 'Connection nahi ho paaya — dobara try karein');
      this.connectBtn.disabled = false;
      this.connectBtn.textContent = 'Connect Karein';
    }
  }

  async disconnect() {
    if (this.client) {
      await this.client.disconnect();
    }
  }

  setupAudio() {
    this.client.on(RTVIEvent.TrackStarted, (track, participant) => {
      if (!participant?.local && track.kind === 'audio') {
        const audio = document.createElement('audio');
        audio.autoplay = true;
        audio.srcObject = new MediaStream([track]);
        document.body.appendChild(audio);
      }
    });
  }

  onConnected() {
    this.isConnected = true;
    this.connectBtn.disabled = false;
    this.connectBtn.textContent = 'Disconnect';
    this.connectBtn.classList.add('is-connected');
    this.micBtn.disabled = false;

    // `enableMic: true` in the PipecatClient constructor only makes the mic
    // available for toggling -- it does NOT start capture on its own (the
    // UI showed "Mic is Off" right after connect, confirmed by testing).
    // A voice assistant should start listening the moment it connects, not
    // require the caller to separately notice and click a mic button, so
    // turn it on explicitly here.
    this.client.enableMic(true);
    this.updateMicButton(this.client.isMicEnabled);

    if (this.textAskInput) this.textAskInput.disabled = false;
    if (this.textAskSend) this.textAskSend.disabled = false;

    this.sessionPill.textContent = 'Connected';
    this.sessionPill.className = 'pill pill-live';
    this.setOrb('idle', 'Connected — bot bolna shuru karega');
    this.startMicMeter();
    this.startAnswersPolling();
  }

  onDisconnected() {
    this.isConnected = false;
    this.connectBtn.textContent = 'Connect Karein';
    this.connectBtn.classList.remove('is-connected');
    this.micBtn.disabled = true;
    this.updateMicButton(false);
    if (this.textAskInput) this.textAskInput.disabled = true;
    if (this.textAskSend) this.textAskSend.disabled = true;
    this.sessionPill.textContent = 'Not connected';
    this.sessionPill.className = 'pill pill-muted';
    this.stopMicMeter();
    this.stopAnswersPolling();
    this.setOrb('idle', 'Baat shuru karne ke liye connect karein');
  }

  // Turns the SDK's structured DeviceError into a human-readable reason,
  // shown right on the mic status label instead of a silent "Mic Off" --
  // this is what actually tells us WHY capture failed on a given machine.
  onDeviceError(error) {
    console.error('Device error:', error?.type, error?.details, error);
    if (!error?.devices?.includes('mic')) return;

    const REASON_TEXT = {
      'in-use': 'Mic doosri app mein use ho raha hai (Zoom/Teams band karke try karein)',
      permissions: 'Mic permission block hai — browser ke address bar mein 🔒/site-settings se allow karein',
      'undefined-mediadevices': 'Browser mic access support nahi kar raha (HTTPS ya localhost par kholiye)',
      'not-found': 'Koi microphone detect nahi hua — device connect hai check karein',
      constraints: 'Mic ki settings is browser ke saath match nahi ho rahi',
      unknown: 'Mic capture fail hui — console mein detail dekhein',
    };
    const message = REASON_TEXT[error?.type] || REASON_TEXT.unknown;

    if (this.micStatus) this.micStatus.textContent = message;
    if (this.micLabel) this.micLabel.textContent = 'Mic Error';
    if (this.micIcon) this.micIcon.textContent = '⚠️';
    this.setOrb('idle', message);
  }

  updateMicButton(enabled) {
    this.micLabel.textContent = enabled ? 'Mic On' : 'Mic Off';
    this.micIcon.textContent = enabled ? '🎤' : '🔇';
    this.micBtn.classList.toggle('is-active', enabled);
    this.micStatus.textContent = enabled ? 'Mic is On' : 'Mic is Off';
  }

  setOrb(state, statusText) {
    if (this.orb) this.orb.className = `orb orb-${state}`;
    if (this.orbStatus && statusText) this.orbStatus.textContent = statusText;
  }

  // Live meter of the actual outgoing mic signal -- same approach as
  // kyc-voice-agent's kycClient.js: the one objective way to tell "the
  // browser captured real audio" from "permission granted but the track is
  // silent" without server-side logs.
  startMicMeter() {
    const track = this.client?.tracks?.().local?.audio;
    if (!track) {
      if ((this._micMeterRetries ?? 0) < 10) {
        this._micMeterRetries = (this._micMeterRetries ?? 0) + 1;
        setTimeout(() => this.startMicMeter(), 300);
      }
      return;
    }
    this._micMeterRetries = 0;
    if (!this.micMeterFill) return;

    try {
      const AudioContextCtor = window.AudioContext || window.webkitAudioContext;
      this.meterAudioContext = new AudioContextCtor();
      const source = this.meterAudioContext.createMediaStreamSource(new MediaStream([track]));
      this.meterAnalyser = this.meterAudioContext.createAnalyser();
      this.meterAnalyser.fftSize = 512;
      source.connect(this.meterAnalyser);
      this.meterData = new Uint8Array(this.meterAnalyser.frequencyBinCount);

      const tick = () => {
        if (!this.meterAnalyser) return;
        this.meterAnalyser.getByteTimeDomainData(this.meterData);
        let sumSquares = 0;
        for (let i = 0; i < this.meterData.length; i++) {
          const v = (this.meterData[i] - 128) / 128;
          sumSquares += v * v;
        }
        const rms = Math.sqrt(sumSquares / this.meterData.length);
        const pct = Math.min(100, Math.round(rms * 400));
        this.micMeterFill.style.width = `${pct}%`;
        this._meterFrame = requestAnimationFrame(tick);
      };
      tick();
    } catch (error) {
      console.warn('Mic meter error:', error);
    }
  }

  stopMicMeter() {
    if (this._meterFrame) {
      cancelAnimationFrame(this._meterFrame);
      this._meterFrame = null;
    }
    if (this.meterAudioContext) {
      this.meterAudioContext.close().catch(() => {});
      this.meterAudioContext = null;
    }
    this.meterAnalyser = null;
    if (this.micMeterFill) this.micMeterFill.style.width = '0%';
  }

  addConversationMessage(text, role) {
    if (!text) return;
    const empty = this.conversationLog.querySelector('.transcript-empty');
    if (empty) empty.remove();

    const messageDiv = document.createElement('div');
    messageDiv.className = `conversation-message ${role}`;
    const roleSpan = document.createElement('div');
    roleSpan.className = 'role';
    roleSpan.textContent = role === 'user' ? 'Aap' : 'Munshi';
    const textDiv = document.createElement('div');
    textDiv.textContent = text;
    messageDiv.appendChild(roleSpan);
    messageDiv.appendChild(textDiv);
    this.conversationLog.appendChild(messageDiv);
    this.conversationLog.scrollTop = this.conversationLog.scrollHeight;
  }

  // Polls /session/{id}/answers (public, no admin key -- the caller's own
  // session's own data) so the output panel picks up each ask's structured
  // calculation-engine result once grounding.finalize_turn() has logged it
  // server-side. Same polling philosophy as kyc-voice-agent's
  // session-status poll: the tool result isn't pushed to the frontend
  // directly, so this is how the UI learns what happened.
  startAnswersPolling() {
    this.stopAnswersPolling();
    this.lastAnswerCount = 0;
    this.refreshAnswers();
    this.answersPollTimer = setInterval(() => this.refreshAnswers(), ANSWERS_POLL_INTERVAL_MS);
  }

  stopAnswersPolling() {
    if (this.answersPollTimer) {
      clearInterval(this.answersPollTimer);
      this.answersPollTimer = null;
    }
  }

  async refreshAnswers() {
    if (!this.sessionId) return;
    try {
      const resp = await fetch(`${SERVER_URL}/session/${this.sessionId}/answers`);
      if (!resp.ok) return;
      const answers = await resp.json();
      if (answers.length !== this.lastAnswerCount) {
        this.lastAnswerCount = answers.length;
        this.callbacks.onAnswers?.(answers);
      }
    } catch {
      // transient network hiccup -- next poll tick retries
    }
  }
}
