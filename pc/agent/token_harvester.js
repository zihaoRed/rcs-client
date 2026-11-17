// agent/token_harvester.js — long-lived harvester for Tachyon client-info token
const mod = require('frida-java-bridge');
const Java = mod.default || mod;

function emit(o) { if (typeof globalThis.send === 'function') globalThis.send(o); }
function jstr(s) { try { return String(s); } catch (e) { return ''; } }
function methodOf(url) {
  const i = url.indexOf('instantmessaging');
  if (i < 0) return url;
  const j = url.indexOf('/', i + 'instantmessaging'.length);
  return j >= 0 ? url.slice(j + 1) : url;
}
function isTachyon(u) { return u.indexOf('instantmessaging') >= 0; }

function headerPairs(arr) {
  const out = {};
  try {
    for (let i = 0; i + 1 < arr.length; i += 2) out[jstr(arr[i]).toLowerCase()] = jstr(arr[i + 1]);
  } catch (e) {}
  return out;
}

Java.perform(function () {
  const BS = Java.use('android.net.http.internal.org.chromium.net.impl.CronetBidirectionalStream');
  const seen = new Set();

  function urlOf(self) { try { return jstr(self.mInitialUrl.value); } catch (e) { return ''; } }

  BS.start.implementation = function () {
    let u = '';
    try { u = urlOf(this); } catch (e) {}
    if (isTachyon(u)) {
      let hdrs = {};
      try { hdrs = headerPairs(this.mRequestHeaders.value); } catch (e) {}
      const token = hdrs['x-goog-ext-202964622-bin'] || '';
      const method = methodOf(u);
      emit({ k: 'start', m: method, hasToken: !!token, ts: Date.now(), pid: Process.id });
      if (token) emit({ k: 'token', m: method, token: token, ts: Date.now(), pid: Process.id });
      if (method.indexOf('ReceiveMessages') >= 0 || method.indexOf('PullMessages') >= 0 ||
          method.indexOf('AckMessages') >= 0) {
        emit({ k: 'reqstart', m: method, hdrs: hdrs, ts: Date.now(), pid: Process.id });
      }
    }
    return this.start();
  };

  BS.write.implementation = function (bb, eos) {
    let u = '';
    try { u = urlOf(this); } catch (e) {}
    if (isTachyon(u)) {
      const m = methodOf(u);
      if (m.indexOf('SendMessage') >= 0 || m.indexOf('PullMessages') >= 0 ||
          m.indexOf('ReceiveMessages') >= 0 || m.indexOf('AckMessages') >= 0 || m.indexOf('PrewarmReceiver') >= 0) {
        try {
          const BB = Java.use('java.nio.ByteBuffer');
          const n = Math.min(bb.remaining(), 8192);
          const dup = bb.duplicate();
          const arr = Java.array('byte', new Array(n).fill(0));
          BB.get.overload('[B', 'int', 'int').call(dup, arr, 0, n);
          let hex = '';
          for (let i = 0; i < n; i++) { const b = arr[i] & 0xff; hex += (b < 16 ? '0' : '') + b.toString(16); }
          let kind = 'write';
          if (m.indexOf('Registration') >= 0) kind = 'regwrite';
          else if (m.indexOf('SendMessage') >= 0) kind = 'sendwrite';
          else if (m.indexOf('PullMessages') >= 0) kind = 'pullwrite';
          else if (m.indexOf('ReceiveMessages') >= 0) kind = 'recvwrite';
          else if (m.indexOf('AckMessages') >= 0) kind = 'ackwrite';
          else if (m.indexOf('PrewarmReceiver') >= 0) kind = 'prewarmwrite';
          emit({ k: kind, m: m, eos: !!eos, len: bb.remaining(), hex: hex, ts: Date.now(), pid: Process.id });
        } catch (e) {}
      }
    }
    return this.write(bb, eos);
  };

  try {
    BS.flush.implementation = function () {
      let u = '';
      try { u = urlOf(this); } catch (e) {}
      if (u.indexOf('PullMessages') >= 0) {
        emit({ k: 'pullflush', m: methodOf(u), ts: Date.now(), pid: Process.id });
      }
      return this.flush();
    };
  } catch (e) {}

  // Best-effort response capture (used to study RegisterRefresh responses)
  try {
    BS.onReadCompleted.overloads.forEach(function (ov) {
      ov.implementation = function () {
        try {
          const u = urlOf(this);
          if (isTachyon(u)) {
            const BB = Java.use('java.nio.ByteBuffer');
            const nums = [];
            for (let i = 0; i < arguments.length; i++) {
              if (typeof arguments[i] === 'number') nums.push(arguments[i]);
            }
            for (let i = 0; i < arguments.length; i++) {
              const a = arguments[i];
              if (a !== null && typeof a === 'object' && a.getClass && /ByteBuffer/.test(a.getClass().getName())) {
                const bb = Java.cast(a, BB);
                let count = nums.length >= 3 ? nums[0] : bb.remaining();
                let start = nums.length >= 3 ? nums[1] : 0;
                if (count <= 0 || count > 1 << 20) count = bb.remaining();
                if (start < 0 || start > bb.remaining()) start = 0;
                const cap = methodOf(u).indexOf('Registration') >= 0 ? 65536 : 8192;
                const want = Math.min(count, cap);
                const dup = bb.duplicate();
                try { dup.position(start); } catch (e) { start = 0; }
                const arr = Java.array('byte', new Array(want).fill(0));
                BB.get.overload('[B', 'int', 'int').call(dup, arr, 0, want);
                let hex = '';
                for (let j = 0; j < want; j++) { const b = arr[j] & 0xff; hex += (b < 16 ? '0' : '') + b.toString(16); }
                emit({ k: 'read', m: methodOf(u), len: count, start: start, nums: nums.slice(0, 4), hex: hex, ts: Date.now(), pid: Process.id });
              }
            }
          }
        } catch (e) {}
        return ov.apply(this, arguments);
      };
    });
  } catch (e) {}

  try {
    BS.onResponseHeadersReceived.overloads.forEach(function (ov) {
      ov.implementation = function () {
        try {
          const u = urlOf(this);
          if (isTachyon(u)) {
            for (let i = 0; i < arguments.length; i++) {
              const a = arguments[i];
              if (a !== null && a.getClass && a.getClass().getName() === '[Ljava.lang.String;') {
                const h = headerPairs(a);
                const m = methodOf(u);
                if (h['x-goog-ext-202964622-bin']) {
                  emit({ k: 'respToken', m: m, token: h['x-goog-ext-202964622-bin'], ts: Date.now(), pid: Process.id });
                }
                if (m.indexOf('Registration') >= 0) {
                  emit({ k: 'respHeaders', m: m, hdrs: h, ts: Date.now(), pid: Process.id });
                }
              }
            }
          }
        } catch (e) {}
        return ov.apply(this, arguments);
      };
    });
  } catch (e) {}

  try {
    BS.onResponseTrailersReceived.overloads.forEach(function (ov) {
      ov.implementation = function () {
        try {
          const u = urlOf(this);
          if (isTachyon(u) && methodOf(u).indexOf('Registration') >= 0) {
            for (let i = 0; i < arguments.length; i++) {
              const a = arguments[i];
              if (a !== null && a.getClass && a.getClass().getName() === '[Ljava.lang.String;') {
                emit({ k: 'trailers', m: methodOf(u), hdrs: headerPairs(a), ts: Date.now(), pid: Process.id });
              }
            }
          }
        } catch (e) {}
        return ov.apply(this, arguments);
      };
    });
  } catch (e) {}

  emit({ k: 'ready', pid: Process.id });
});
