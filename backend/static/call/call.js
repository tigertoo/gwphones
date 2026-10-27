// Call link page: calls the link's extension or ring group from the browser
// with JsSIP over the PBX's WebSocket. Nothing happens until Call is pressed.
(function () {
  "use strict";
  const cfg = JSON.parse(document.getElementById("config").textContent);
  const $ = (id) => document.getElementById(id);
  const callBtn = $("call"), hangupBtn = $("hangup"), muteBtn = $("mute"), status = $("status");
  let ua = null, session = null, stream = null, timer = null, started = 0;

  JsSIP.debug.disable("JsSIP:*");

  const say = (text) => { status.textContent = text; };
  const show = (calling) => {
    callBtn.hidden = calling;
    hangupBtn.hidden = !calling;
    muteBtn.hidden = !calling;
    $("name").disabled = calling;
    if ($("video")) $("video").disabled = calling;
  };

  const REASONS = {
    "Busy": "The line is busy. Please try again shortly.",
    "Not Found": "This call link isn't available any more.",
    "Unavailable": "Nobody can take the call right now. Please try again later.",
    "Rejected": "The call wasn't accepted.",
    "No Answer": "There was no answer. Please try again later.",
    "User Denied Media Access": "The browser wasn't allowed to use the microphone.",
    "Connection Error": "Couldn't reach the phone system. Check your connection and try again.",
    "Request Timeout": "The phone system didn't answer in time. Please try again.",
  };

  function cleanUp(text) {
    clearInterval(timer);
    timer = null;
    if (stream) stream.getTracks().forEach((t) => t.stop());
    stream = null;
    session = null;
    $("videos").hidden = true;
    $("remote").srcObject = null;
    $("local").srcObject = null;
    $("audio").srcObject = null;
    muteBtn.textContent = "Mute";
    show(false);
    if (ua) { ua.stop(); ua = null; }
    say(text);
  }

  function tick() {
    const s = Math.floor((Date.now() - started) / 1000);
    say(`On the call · ${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`);
  }

  function connect(name) {
    return new Promise((resolve, reject) => {
      const socket = new JsSIP.WebSocketInterface(cfg.ws);
      ua = new JsSIP.UA({
        sockets: [socket],
        uri: `sip:${cfg.user}@${cfg.domain}`,
        password: cfg.password,
        display_name: name || "Web visitor",
        register: false,
        session_timers: false,
        user_agent: "Call link",
      });
      const t = setTimeout(() => reject(new Error("Connection Error")), 10000);
      ua.on("connected", () => { clearTimeout(t); resolve(); });
      ua.on("disconnected", () => { if (!session) { clearTimeout(t); reject(new Error("Connection Error")); } });
      ua.start();
    });
  }

  callBtn.addEventListener("click", async () => {
    const wantVideo = !!(cfg.video && $("video") && $("video").checked);
    const name = $("name").value.replace(/[^A-Za-z0-9 ._]/g, "").slice(0, 24).trim();
    show(true);
    say("Asking for your microphone…");
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: wantVideo });
    } catch {
      cleanUp(wantVideo ? "The browser wasn't allowed to use the microphone or camera." :
        "The browser wasn't allowed to use the microphone.");
      return;
    }
    if (wantVideo) {
      $("local").srcObject = stream;
      $("videos").hidden = false;
    }
    say("Connecting…");
    try {
      await connect(name);
    } catch {
      cleanUp(REASONS["Connection Error"]);
      return;
    }
    say("Calling…");
    session = ua.call(`sip:${cfg.target}@${cfg.domain}`, {
      mediaStream: stream,
      // No STUN: the PBX has a public address, so the browser's own
      // addresses are enough, and no third party sees the call.
      pcConfig: { iceServers: [] },
      rtcOfferConstraints: { offerToReceiveAudio: true, offerToReceiveVideo: wantVideo },
      eventHandlers: {
        peerconnection: (e) => {
          e.peerconnection.addEventListener("track", (ev) => {
            const [remote] = ev.streams;
            if (ev.track.kind === "video") {
              $("remote").srcObject = remote;
              $("videos").hidden = false;
            } else {
              $("audio").srcObject = remote;
            }
          });
        },
        progress: () => say("Ringing…"),
        confirmed: () => {
          started = Date.now();
          tick();
          timer = setInterval(tick, 1000);
        },
        ended: () => cleanUp("Call ended. Thank you."),
        failed: (e) => cleanUp(REASONS[e.cause] || `The call didn't go through (${e.cause}).`),
      },
    });
  });

  hangupBtn.addEventListener("click", () => {
    if (session) session.terminate();
    else cleanUp("Call ended.");
  });

  muteBtn.addEventListener("click", () => {
    if (!session) return;
    if (session.isMuted().audio) {
      session.unmute({ audio: true });
      muteBtn.textContent = "Mute";
    } else {
      session.mute({ audio: true });
      muteBtn.textContent = "Unmute";
    }
  });

  window.addEventListener("beforeunload", () => { if (session) session.terminate(); });
})();
