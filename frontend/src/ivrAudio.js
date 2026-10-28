// IVR greetings in the browser: any audio file the browser can play, or a
// recording from the microphone, is turned into what Asterisk plays as is
// (8 kHz mono G.711 mu-law, no header), so the server never has to read
// an uploaded audio file. Phone calls are 8 kHz anyway.

const RATE = 8000;
export const MAX_SECONDS = 120;

function linearToMulaw(sample) {
  const BIAS = 0x84, CLIP = 32635;
  let s = Math.max(-32768, Math.min(32767, Math.round(sample * 32767)));
  const sign = s < 0 ? 0x80 : 0;
  if (sign) s = -s;
  if (s > CLIP) s = CLIP;
  s += BIAS;
  let exponent = 7;
  for (let mask = 0x4000; (s & mask) === 0 && exponent > 0; exponent--, mask >>= 1);
  const mantissa = (s >> (exponent + 3)) & 0x0f;
  return ~(sign | (exponent << 4) | mantissa) & 0xff;
}

function mulawToLinear(u) {
  u = ~u & 0xff;
  const sign = u & 0x80, exponent = (u >> 4) & 0x07, mantissa = u & 0x0f;
  const s = (((mantissa << 3) + 0x84) << exponent) - 0x84;
  return (sign ? -s : s) / 32768;
}

// An audio file (ArrayBuffer) -> Uint8Array of mu-law. Throws an Error
// whose message can be shown as it is.
export async function toMulaw(data) {
  const ctx = new AudioContext();
  let decoded;
  try {
    decoded = await ctx.decodeAudioData(data);
  } catch {
    throw new Error("This browser can't read that file. Try a WAV or MP3.");
  } finally {
    ctx.close();
  }
  if (decoded.duration > MAX_SECONDS) throw new Error(`Greetings can be up to ${MAX_SECONDS / 60} minutes long.`);
  if (decoded.duration < 0.5) throw new Error("That greeting is too short.");
  // Mix down to mono and resample to 8 kHz.
  const offline = new OfflineAudioContext(1, Math.ceil(decoded.duration * RATE), RATE);
  const src = offline.createBufferSource();
  src.buffer = decoded;
  src.connect(offline.destination);
  src.start();
  const pcm = (await offline.startRendering()).getChannelData(0);
  // Even out the volume: loudest point at about -1 dB.
  let peak = 0;
  for (const v of pcm) peak = Math.max(peak, Math.abs(v));
  const gain = peak > 0.01 ? 0.89 / peak : 1;
  const out = new Uint8Array(pcm.length);
  for (let i = 0; i < pcm.length; i++) out[i] = linearToMulaw(pcm[i] * gain);
  return out;
}

// Plays mu-law bytes; returns a function that stops them.
export function playMulaw(bytes, onEnd) {
  const ctx = new AudioContext();
  const buf = ctx.createBuffer(1, bytes.length, RATE);
  const ch = buf.getChannelData(0);
  for (let i = 0; i < bytes.length; i++) ch[i] = mulawToLinear(bytes[i]);
  const src = ctx.createBufferSource();
  src.buffer = buf;
  src.connect(ctx.destination);
  src.onended = () => { ctx.close(); onEnd?.(); };
  src.start();
  return () => { try { src.stop(); } catch { /* already stopped */ } };
}

// Starts recording from the microphone; resolves to a function that stops
// and resolves to the recording as mu-law bytes.
export async function startRecording() {
  const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: false, noiseSuppression: true } });
  const recorder = new MediaRecorder(stream);
  const chunks = [];
  recorder.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
  const done = new Promise((resolve) => { recorder.onstop = resolve; });
  recorder.start();
  return async () => {
    recorder.stop();
    await done;
    stream.getTracks().forEach((t) => t.stop());
    return toMulaw(await new Blob(chunks, { type: recorder.mimeType }).arrayBuffer());
  };
}
