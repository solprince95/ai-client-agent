(function () {
  "use strict";

  var scriptTag = document.currentScript;
  var clinicId = scriptTag ? scriptTag.getAttribute("data-clinic-id") : null;
  var apiBase = "https://vajralabs.co.in";

  if (!clinicId) {
    console.error("Vajra Labs chat widget: missing data-clinic-id on the script tag.");
    return;
  }

  var storageKey = "vajra_conv_" + clinicId;

  var state = {
    conversationId: null,
    consentGiven: false,
    open: false,
  };

  function loadSavedState() {
    try {
      var raw = localStorage.getItem(storageKey);
      if (!raw) return null;
      return JSON.parse(raw);
    } catch (e) {
      return null;
    }
  }

  function saveState() {
    try {
      localStorage.setItem(storageKey, JSON.stringify({
        conversationId: state.conversationId,
        consentGiven: state.consentGiven,
      }));
    } catch (e) {
      // localStorage unavailable (private browsing, etc.) - not fatal,
      // just means this visitor won't be remembered across page loads.
    }
  }

  var css = "" +
    "#vajra-chat-bubble{position:fixed;bottom:20px;right:20px;width:58px;height:58px;border-radius:50%;background:#1d6aff;box-shadow:0 4px 18px rgba(0,0,0,.2);cursor:pointer;display:flex;align-items:center;justify-content:center;z-index:999999;border:none;}" +
    "#vajra-chat-bubble svg{width:26px;height:26px;}" +
    "#vajra-chat-panel{position:fixed;bottom:90px;right:20px;width:340px;max-width:92vw;height:460px;max-height:75vh;background:#fff;border-radius:14px;box-shadow:0 10px 40px rgba(0,0,0,.25);display:none;flex-direction:column;overflow:hidden;z-index:999999;font-family:system-ui,-apple-system,sans-serif;}" +
    "#vajra-chat-panel.open{display:flex;}" +
    "#vajra-chat-header{background:#1d6aff;color:#fff;padding:14px 16px;font-size:14px;font-weight:600;}" +
    "#vajra-chat-messages{flex:1;overflow-y:auto;padding:14px;background:#f7f8fa;}" +
    ".vajra-msg{max-width:80%;padding:8px 12px;border-radius:12px;margin-bottom:8px;font-size:13.5px;line-height:1.4;white-space:pre-wrap;}" +
    ".vajra-msg.bot{background:#fff;color:#1a1a2e;border:1px solid #e5e7eb;border-bottom-left-radius:2px;}" +
    ".vajra-msg.visitor{background:#1d6aff;color:#fff;margin-left:auto;border-bottom-right-radius:2px;}" +
    "#vajra-chat-consent{padding:14px;background:#fff;border-top:1px solid #eee;font-size:12px;color:#555;}" +
    "#vajra-chat-consent button{margin-top:8px;background:#1d6aff;color:#fff;border:none;border-radius:8px;padding:8px 14px;font-size:13px;cursor:pointer;}" +
    "#vajra-chat-inputrow{display:flex;border-top:1px solid #eee;padding:10px;background:#fff;}" +
    "#vajra-chat-input{flex:1;border:1px solid #ddd;border-radius:20px;padding:8px 14px;font-size:13.5px;outline:none;}" +
    "#vajra-chat-send{margin-left:8px;background:#1d6aff;color:#fff;border:none;border-radius:50%;width:36px;height:36px;cursor:pointer;flex-shrink:0;}" +
    "#vajra-chat-typing{font-size:12px;color:#999;padding:0 14px 8px;}";

  var styleEl = document.createElement("style");
  styleEl.textContent = css;
  document.head.appendChild(styleEl);

  var bubble = document.createElement("button");
  bubble.id = "vajra-chat-bubble";
  bubble.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>';
  document.body.appendChild(bubble);

  var panel = document.createElement("div");
  panel.id = "vajra-chat-panel";
  panel.innerHTML =
    '<div id="vajra-chat-header">Chat with us</div>' +
    '<div id="vajra-chat-messages"></div>' +
    '<div id="vajra-chat-typing" style="display:none;">Typing...</div>' +
    '<div id="vajra-chat-consent" style="display:none;">' +
      "By continuing, you agree to be contacted about your enquiry. " +
      '<button id="vajra-chat-consent-btn">I agree, continue</button>' +
    "</div>" +
    '<div id="vajra-chat-inputrow" style="display:none;">' +
      '<input id="vajra-chat-input" type="text" placeholder="Type a message..." />' +
      '<button id="vajra-chat-send" aria-label="Send">' +
        '<svg viewBox="0 0 24 24" width="16" height="16" fill="white"><path d="M2 21l21-9L2 3v7l15 2-15 2z"/></svg>' +
      "</button>" +
    "</div>";
  document.body.appendChild(panel);

  var messagesEl = document.getElementById("vajra-chat-messages");
  var consentEl = document.getElementById("vajra-chat-consent");
  var inputRowEl = document.getElementById("vajra-chat-inputrow");
  var inputEl = document.getElementById("vajra-chat-input");
  var typingEl = document.getElementById("vajra-chat-typing");

  function addMessage(role, text) {
    var div = document.createElement("div");
    div.className = "vajra-msg " + role;
    div.textContent = text;
    messagesEl.appendChild(div);
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function setTyping(on) {
    typingEl.style.display = on ? "block" : "none";
  }

  function openPanel() {
    panel.classList.add("open");
    state.open = true;
    if (!state.conversationId) {
      var saved = loadSavedState();
      if (saved && saved.conversationId) {
        state.conversationId = saved.conversationId;
        state.consentGiven = !!saved.consentGiven;
        addMessage("bot", "Welcome back! How can we help?");
        if (state.consentGiven) {
          inputRowEl.style.display = "flex";
          inputEl.focus();
        } else {
          consentEl.style.display = "block";
        }
      } else {
        startConversation();
      }
    }
  }

  function togglePanel() {
    if (state.open) {
      panel.classList.remove("open");
      state.open = false;
    } else {
      openPanel();
    }
  }

  bubble.addEventListener("click", togglePanel);

  function startConversation() {
    setTyping(true);
    fetch(apiBase + "/api/widget/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ clinic_id: clinicId }),
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        setTyping(false);
        if (!data.ok) {
          addMessage("bot", "Chat isn't available right now, please try again later.");
          return;
        }
        state.conversationId = data.conversation_id;
        addMessage("bot", data.greeting);
        consentEl.style.display = "block";
        saveState();
      })
      .catch(function () {
        setTyping(false);
        addMessage("bot", "Something went wrong, please refresh and try again.");
      });
  }

  document.getElementById("vajra-chat-consent-btn").addEventListener("click", function () {
    fetch(apiBase + "/api/widget/consent", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ conversation_id: state.conversationId }),
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.ok) {
          state.consentGiven = true;
          consentEl.style.display = "none";
          inputRowEl.style.display = "flex";
          inputEl.focus();
          saveState();
        }
      });
  });

  function sendMessage() {
    var text = inputEl.value.trim();
    if (!text || !state.consentGiven) return;
    addMessage("visitor", text);
    inputEl.value = "";
    setTyping(true);
    fetch(apiBase + "/api/widget/message", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ conversation_id: state.conversationId, message: text }),
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        setTyping(false);
        if (data.human_takeover) {
          return; // a staff member has taken over, bot stays quiet, staff replies come through a future poll
        }
        if (!data.ok && /could not be found/i.test(data.reply || "")) {
          // The conversation ID we had (likely from a stale localStorage
          // entry, e.g. from before this device remembered conversations,
          // or a conversation that's since been deleted) no longer
          // exists server-side. Recover automatically instead of leaving
          // the visitor stuck on a dead end forever.
          try { localStorage.removeItem(storageKey); } catch (e) {}
          state.conversationId = null;
          state.consentGiven = false;
          addMessage("bot", "Sorry about that, let's start fresh. One moment...");
          startConversation();
          return;
        }
        if (data.reply) {
          addMessage("bot", data.reply);
        } else if (!data.ok) {
          addMessage("bot", data.reply || "Something went wrong, please try again.");
        }
      })
      .catch(function () {
        setTyping(false);
        addMessage("bot", "Something went wrong, please try again.");
      });
  }

  document.getElementById("vajra-chat-send").addEventListener("click", sendMessage);
  inputEl.addEventListener("keydown", function (e) {
    if (e.key === "Enter") sendMessage();
  });
})();
