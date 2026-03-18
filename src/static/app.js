// =============================
// UI ELEMENTS
// =============================
const startBtn = document.getElementById('startBtn');
const canvas = document.getElementById('visualizer');
const canvasCtx = canvas ? canvas.getContext('2d') : null;
const stopBtn = document.getElementById('stopBtn');
const statusBox = document.getElementById('statusBox');
const statusText = document.getElementById('statusText');
const statusMsg = document.getElementById('statusMsg');
const logEl = document.getElementById('log');
const chatContainer = document.getElementById('chatContainer');
const exportBtn = document.getElementById('exportBtn');

// =============================
// CONFIGURATION & STATE
// =============================
const TARGET_RATE = 24000;
const CHUNK_DURATION_MS = 150;
const MAX_LOG_LINES = 250;

let eventSource = null;
let micAnalyser = null;
let assistantAnalyser = null;
let visualizerAnimationId = null;
let wsAudio = null;
let stopped = false;

let micStream = null;
let audioContext = null;
let processorNode = null;
let capturing = false;
let pendingFloat = [];
let inputSampleRate = 48000;

let nextPlayTime = 0;
let suspendPlayback = false;
let assistantSources = [];
let readySince = null;

function log(msg, level='info', obj){
  const line = document.createElement('div');
  line.className = level === 'error' ? 'err' : level === 'debug' ? 'dbg' : level === 'warn' ? 'warn' : '';
  const ts = new Date().toISOString().split('T')[1].replace('Z','');
  line.textContent = `[${ts} ${level}] ${msg}`;
  if(obj) {
    line.title = typeof obj === 'string' ? obj : JSON.stringify(obj).slice(0,300);
  }
  logEl.appendChild(line);
  while(logEl.children.length > MAX_LOG_LINES) logEl.removeChild(logEl.firstChild);
  logEl.scrollTop = logEl.scrollHeight;
}

function updateStatusUI(data){
  if(!data) return;
  statusText.textContent = data.state;
  statusMsg.textContent = data.message || '';
  const s = data.state;
  let colorVar = 'var(--c-status-idle)';
  if(['starting','processing'].includes(s)) colorVar = 'var(--c-status-processing)';
  else if(s === 'assistant_speaking') colorVar = 'var(--c-status-speaking)';
  else if(s === 'listening') colorVar = 'var(--c-status-listening)';
  else if(s === 'error') colorVar = 'var(--c-status-error)';
  else if(s === 'ready') colorVar = 'var(--c-status-ready)';
  else if(s === 'stopped') colorVar = 'var(--c-status-stopped)';
  statusBox.style.background = colorVar;
  if(s === 'stopped' || s === 'idle' || s === 'error') {
    stopMicCapture();
    stopBtn.disabled = true;
    startBtn.disabled = false;
    startBtn.textContent = 'Start Session';
  }
  if(s === 'ready') {
    if(!readySince) readySince = performance.now();
  } else {
    readySince = null;
  }
  if(s === 'ready' && !capturing && !stopped) {
    startMicCapture().catch(e=>log('Mic capture failed: '+e,'error'));
  }
}

// =============================
// NEW: CHAT UI HANDLING
// =============================
function renderChatMessage(msg) {
    // msg = { role: "user" | "assistant", text: "..." }
    
    // Remove placeholder if present
    const placeholder = chatContainer.querySelector('.chat-placeholder');
    if(placeholder) placeholder.remove();

    const bubble = document.createElement('div');
    bubble.className = `chat-bubble ${msg.role === 'user' ? 'chat-user' : 'chat-assistant'}`;
    bubble.textContent = msg.text;
    
    chatContainer.appendChild(bubble);
    chatContainer.scrollTop = chatContainer.scrollHeight;
}

// =============================
// SSE HANDLING
// =============================
function openEventSource(){
  if(eventSource){ eventSource.close(); }
  const url = '/events?session_id=' + encodeURIComponent(window.SESSION_ID || '');
  eventSource = new EventSource(url);
  eventSource.onmessage = handleSSEMessage;
  eventSource.onerror = () => log('SSE connection error (will retry if closed).','warn');
  log('SSE connection opened');
}

function handleSSEMessage(ev) {
  if(!ev.data) return;
  
  try {
    const data = JSON.parse(ev.data);
    
    switch(data.type) {
      case 'status':
        handleStatusUpdate(data);
        break;
      case 'audio':
        handleAudioData(data);
        break;
      case 'chat_message': // NEW CASE
        renderChatMessage(data.message);
        break;
      case 'log':
        log(data.msg || data.event_type || JSON.stringify(data), data.level || 'info');
        break;
      case 'control':
        handleControlEvent(data);
        break;
    }
  } catch(e){
    log('Bad SSE message: '+ e,'error');
  }
}

function handleStatusUpdate(data) {
  updateStatusUI(data);
  if(data.state === 'ready' || data.state === 'assistant_speaking') {
    if(suspendPlayback) {
      log('Resuming assistant playback (' + data.state + ')','debug');
      suspendPlayback = false;
    }
  }
}

function handleAudioData(data) {
  if(suspendPlayback) return;
  playAssistantPcm16(data.audio);
}

function handleControlEvent(data) {
  if(data.action === 'stop_playback'){
    try { stopAllAssistantPlayback(); } catch(_){ }
    suspendPlayback = true;
    log('Received stop_playback control from server; suspending playback until ready','debug');
  }
}

// =============================
// AUDIO CAPTURE & ENCODE
// =============================
function openAudioWebSocket(){
  try{
    const protocol = location.protocol === 'https:' ? 'wss://' : 'ws://';
    const wsUrl = `${protocol}${location.host}/ws-audio?session_id=${encodeURIComponent(window.SESSION_ID || '')}`;
    
    wsAudio = new WebSocket(wsUrl);
    wsAudio.binaryType = 'arraybuffer';
    wsAudio.onopen = () => log('Audio websocket opened','debug');
    wsAudio.onerror = (e) => { log('Audio websocket error','warn'); wsAudio = null; };
    wsAudio.onclose = () => { log('Audio websocket closed','debug'); wsAudio = null; };
  }catch(e){ wsAudio = null; }
}
function ensureAudioContext(){
  if(!audioContext){
    audioContext = new (window.AudioContext || window.webkitAudioContext)({sampleRate: 48000});
    inputSampleRate = audioContext.sampleRate;
    nextPlayTime = audioContext.currentTime;
    
    micAnalyser = audioContext.createAnalyser();
    micAnalyser.fftSize = 256;
    
    assistantAnalyser = audioContext.createAnalyser();
    assistantAnalyser.fftSize = 256;
    
    if (canvasCtx) drawVisualizer();
  }
}

async function startMicCapture(){
  if(capturing) return;
  ensureAudioContext();
  log('Requesting microphone…');
  micStream = await navigator.mediaDevices.getUserMedia({audio: { echoCancellation:true, noiseSuppression:true, channelCount:1 }, video:false});
  const source = audioContext.createMediaStreamSource(micStream);
  const BUFFER_SIZE = 4096;
  processorNode = audioContext.createScriptProcessor(BUFFER_SIZE, 1, 1);
  let lastSend = performance.now();
  processorNode.onaudioprocess = (ev) => {
    if(!capturing) return;
    const input = ev.inputBuffer.getChannelData(0);
    pendingFloat.push(new Float32Array(input));
    const now = performance.now();
    if(now - lastSend >= CHUNK_DURATION_MS || pendingFloat.length > 12){
      flushPendingAudio();
      lastSend = now;
    }
  };
  source.connect(processorNode);
  processorNode.connect(audioContext.destination);
  source.connect(micAnalyser); // Connect mic to visualizer
  capturing = true;
  log('Microphone capture started');
}

function stopMicCapture(){
  if(!capturing) return;
  capturing = false;
  if(processorNode){ try { processorNode.disconnect(); } catch(_){} }
  if(micStream){
    micStream.getTracks().forEach(t=>t.stop());
    micStream = null;
  }
  pendingFloat = [];
  log('Microphone capture stopped');
}

function mergePendingFloat(){
  if(!pendingFloat.length) return null;
  let total = 0;
  for(const arr of pendingFloat) total += arr.length;
  const merged = new Float32Array(total);
  let offset=0;
  for(const arr of pendingFloat){ merged.set(arr, offset); offset += arr.length; }
  pendingFloat = [];
  return merged;
}

function downsampleToInt16(float32, inRate, outRate){
  if(!float32) return null;
  if(inRate === outRate){
    const int16 = new Int16Array(float32.length);
    for(let i=0;i<float32.length;i++){
      const s = Math.max(-1, Math.min(1, float32[i]));
      int16[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
    }
    return int16;
  }
  const ratio = inRate / outRate;
  const newLen = Math.round(float32.length / ratio);
  const int16 = new Int16Array(newLen);
  for (let i = 0; i < newLen; i++) {
    const srcPos = i * ratio;
    const idx = Math.floor(srcPos);
    const frac = srcPos - idx;
    let s1 = float32[idx] || 0;
    let s2 = float32[idx + 1] || 0;
    const sample = s1 * (1 - frac) + s2 * frac;
    const clamped = Math.max(-1, Math.min(1, sample));
    int16[i] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7FFF;
  }
  return int16;
}

function int16ToBase64(int16){
  if(!int16) return null;
  const bytes = new Uint8Array(int16.buffer);
  let binary='';
  for(let i=0;i<bytes.length;i++) binary += String.fromCharCode(bytes[i]);
  return btoa(binary);
}

function flushPendingAudio(){
  const merged = mergePendingFloat();
  if(!merged || !merged.length) return;
  const int16 = downsampleToInt16(merged, inputSampleRate, TARGET_RATE);
  if(!int16) return;
  try{
    if(wsAudio && wsAudio.readyState === WebSocket.OPEN){
      wsAudio.send(int16.buffer);
      return;
    }
  }catch(e){ }
  const b64 = int16ToBase64(int16);
  if(!b64) return;
  sendAudioChunk(b64);
}

async function sendAudioChunk(b64){
  try {
    const url = '/audio-chunk?session_id=' + encodeURIComponent(window.SESSION_ID || '');
    const r = await fetch(url, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({audio: b64}) });
    if(!r.ok){
      if(r.status === 400) {
        log('Audio chunk rejected: '+ r.status,'warn');
      } else {
        log('Audio send failed: '+ r.status,'warn');
      }
    }
  } catch(e){
    log('Audio send error: '+ e, 'warn');
  }
}

setInterval(()=>{ if(capturing) flushPendingAudio(); }, 500);

// =============================
// ASSISTANT AUDIO PLAYBACK
// =============================
function playAssistantPcm16(b64){
  try {
    ensureAudioContext();
    const binary = atob(b64);
    const bytes = new Uint8Array(binary.length);
    for(let i=0;i<binary.length;i++) bytes[i] = binary.charCodeAt(i);
    const view = new DataView(bytes.buffer);
    const samples = view.byteLength / 2;
    const floatBuf = new Float32Array(samples);
    for(let i=0;i<samples;i++){
      const s = view.getInt16(i*2, true) / 0x8000;
      floatBuf[i] = s;
    }
    const audioBuf = audioContext.createBuffer(1, floatBuf.length, TARGET_RATE);
    audioBuf.copyToChannel(floatBuf, 0, 0);
    const src = audioContext.createBufferSource();
    src.buffer = audioBuf;
    src.connect(assistantAnalyser);
    assistantAnalyser.connect(audioContext.destination);
    assistantSources.push(src);
    src.addEventListener('ended', () => {
      const i = assistantSources.indexOf(src);
      if(i !== -1) assistantSources.splice(i,1);
    });
    const now = audioContext.currentTime;
    if(nextPlayTime < now) nextPlayTime = now + 0.01;
    src.start(nextPlayTime);
    nextPlayTime += audioBuf.duration;
  } catch(e) {
    log('Playback error: '+ e,'error');
  }
}

function stopAllAssistantPlayback(){
  try{
    for(const s of assistantSources.slice()){
      try{ if(typeof s.stop === 'function') s.stop(0); } catch(_){}
      try{ s.disconnect(); } catch(_){}
    }
  }catch(e){ }
  assistantSources = [];
  try{ nextPlayTime = audioContext.currentTime; } catch(_){}
  try{ log('stopAllAssistantPlayback executed at '+ new Date().toISOString(), 'debug'); } catch(_){}
}

// =============================
// SESSION MANAGEMENT
// =============================

async function startSession(){
  stopped = false;
  setSessionButtonState('starting');
  try {
    const url = '/start-session?session_id=' + encodeURIComponent(window.SESSION_ID || '');
    
    // Explicitly grab DOM values
    const voiceSelect = document.getElementById('personaVoice');
    const instructionsInput = document.getElementById('personaInstructions');
    const maxTokensInput = document.getElementById('maxTokens');
    
    const v = voiceSelect ? voiceSelect.value : "alloy";
    const i = instructionsInput ? instructionsInput.value.trim() : "";
    const m = maxTokensInput ? parseInt(maxTokensInput.value, 10) : 500;

    const bodyData = {
        voice: v,
        instructions: i,
        max_tokens: m
    };

    const response = await fetch(url, {
        method:'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(bodyData)
    });
    const result = await response.json();
    if(!response.ok){
      handleStartSessionError(result, response.status);
      return;
    }
    updateStatusUI(result.status || result);
    openEventSource();
    openAudioWebSocket();
    log('Session started successfully');
  } catch(error){
    handleStartSessionError(null, error.message);
  }
}

async function stopSession(){
  stopped = true;
  log('Stopping session…');
  try { 
      const url = '/stop-session?session_id=' + encodeURIComponent(window.SESSION_ID || '');
      await fetch(url, {method:'POST'}); 
  } catch(_){ }
  closeConnections();
  stopMicCapture();
  updateStatusUI({state:'stopped', message:'Session stopped.'});
}

function setSessionButtonState(state) {
  if(state === 'starting') {
    startBtn.disabled = true;
    stopBtn.disabled = false;
    startBtn.textContent = 'Starting…';
  } else if(state === 'stopped') {
    startBtn.disabled = false;
    stopBtn.disabled = true;
    startBtn.textContent = 'Start Session';
  }
}

function handleStartSessionError(result, errorInfo) {
  log('Failed to start: '+ (result?.status?.last_error || errorInfo), 'error');
  if(result) updateStatusUI(result.status || result);
  setSessionButtonState('stopped');
}

function closeConnections() {
  if(eventSource){ eventSource.close(); eventSource = null; }
  if(wsAudio){ wsAudio.close(); wsAudio = null; }
}

startBtn.addEventListener('click', startSession);
stopBtn.addEventListener('click', stopSession);
if (exportBtn) {
    exportBtn.addEventListener('click', () => {
        window.location.href = '/export-transcript?session_id=' + encodeURIComponent(window.SESSION_ID || '');
    });
}
window.addEventListener('beforeunload', closeConnections);

// Dynamic Session Adjustments
async function sendSessionUpdate() {
   if(!capturing || stopped) return; // Only process if session is actively running
   const voiceSelect = document.getElementById('personaVoice');
   const instructionsInput = document.getElementById('personaInstructions');
   const maxTokensInput = document.getElementById('maxTokens');
   
   const v = voiceSelect ? voiceSelect.value : "alloy";
   const i = instructionsInput ? instructionsInput.value.trim() : "";
   const m = maxTokensInput ? parseInt(maxTokensInput.value, 10) : 500;

   const bodyData = {
       voice: v,
       instructions: i,
       max_tokens: m
   };
   
   try {
       const url = '/update-session?session_id=' + encodeURIComponent(window.SESSION_ID || '');
       await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json'}, body: JSON.stringify(bodyData) });
   } catch(e) { log('Failed to push session update: ' + e, 'error'); }
}

const voiceSelectEl = document.getElementById('personaVoice');
if(voiceSelectEl) voiceSelectEl.addEventListener('change', sendSessionUpdate);

const instructionsEl = document.getElementById('personaInstructions');
if(instructionsEl) instructionsEl.addEventListener('change', sendSessionUpdate);

const maxTokensEl = document.getElementById('maxTokens');
if(maxTokensEl) maxTokensEl.addEventListener('change', sendSessionUpdate);

openEventSource();

// =============================
// NOVELTY: AUDIO VISUALIZER
// =============================
function drawVisualizer() {
    visualizerAnimationId = requestAnimationFrame(drawVisualizer);
    if (!canvasCtx || !audioContext) return;

    const WIDTH = canvas.width;
    const HEIGHT = canvas.height;
    
    // Clear canvas
    canvasCtx.clearRect(0, 0, WIDTH, HEIGHT);

    let analyser = null;
    let color = 'rgba(51, 255, 153, 0.2)'; // Idle faint color

    if (capturing && statusText && statusText.textContent === 'Listening...') {
         analyser = micAnalyser;
         color = 'rgba(51, 255, 153, 1)'; // Neon Green
    } else if (assistantSources.length > 0 && !suspendPlayback) {
         analyser = assistantAnalyser;
         color = 'rgba(255, 255, 255, 1)'; // Pure White
    }

    if (!analyser) {
        // Draw flat idle line
        canvasCtx.beginPath();
        canvasCtx.moveTo(0, HEIGHT / 2);
        canvasCtx.lineTo(WIDTH, HEIGHT / 2);
        canvasCtx.lineWidth = 1;
        canvasCtx.strokeStyle = color;
        canvasCtx.stroke();
        return;
    }

    const bufferLength = analyser.frequencyBinCount; // Usually 128
    const dataArray = new Uint8Array(bufferLength);
    analyser.getByteFrequencyData(dataArray);

    const barWidth = Math.max((WIDTH / bufferLength) * 1.5, 2);
    let barHeight;
    let x = 0;

    for (let i = 0; i < bufferLength; i++) {
        // getByteFrequencyData returns 0-255. We scale it down to fit our Canvas HEIGHT nicely.
        barHeight = dataArray[i] * (HEIGHT / 255); 
        // Force minimum height to 2px so the line never fully breaks
        if (barHeight < 2) barHeight = 2; 

        canvasCtx.fillStyle = color;
        // Center the bars vertically 
        canvasCtx.fillRect(x, (HEIGHT - barHeight) / 2, barWidth - 1, barHeight);

        x += barWidth;
        if(x > WIDTH) break;
    }
}

// =============================
// ADLS2 UPLOADER UI LOGIC
// =============================
const dropZone = document.getElementById('dropZone');
const adlsFileInput = document.getElementById('adlsFileInput');
const uploadAdlsBtn = document.getElementById('uploadAdlsBtn');

if (dropZone && adlsFileInput && uploadAdlsBtn) {
    dropZone.addEventListener('click', () => adlsFileInput.click());
    
    dropZone.addEventListener('dragover', (e) => {
        e.preventDefault();
        dropZone.style.borderColor = 'var(--c-gold)';
        dropZone.style.background = 'rgba(6, 4, 0, 0.05)';
    });
    
    dropZone.addEventListener('dragleave', (e) => {
        e.preventDefault();
        dropZone.style.borderColor = 'var(--c-muted)';
        dropZone.style.background = 'rgba(0,0,0,0.2)';
    });
    
    dropZone.addEventListener('drop', (e) => {
        e.preventDefault();
        dropZone.style.borderColor = 'var(--c-muted)';
        dropZone.style.background = 'rgba(0,0,0,0.2)';
        if(e.dataTransfer.files.length) {
            adlsFileInput.files = e.dataTransfer.files;
            uploadAdlsBtn.textContent = `Ready: ${e.dataTransfer.files.length} File(s)`;
            uploadAdlsBtn.style.background = 'rgba(255, 170, 0, 0.1)';
        }
    });

    adlsFileInput.addEventListener('change', () => {
        if(adlsFileInput.files.length) {
            uploadAdlsBtn.textContent = `Ready: ${adlsFileInput.files.length} File(s)`;
            uploadAdlsBtn.style.background = 'rgba(255, 170, 0, 0.1)';
        }
    });
}